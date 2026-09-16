-- Digital Shelf — initial schema for the Cloudflare Worker deployment.
-- Mirrors shop/store.py and shop/supplier_store.py, adapted to Postgres.
-- All tables are service-role only: the Worker holds the service key,
-- the anon key gets nothing.

create table if not exists metadata (
    key text primary key,
    value text not null
);

create table if not exists products (
    sku text primary key,
    title text not null,
    description text not null,
    category text not null,
    price_stars integer not null check (price_stars > 0),
    active boolean not null default true,
    is_demo boolean not null default false,
    source text not null check (source in ('stock', 'supplier'))
);

create table if not exists terms_acceptances (
    user_id bigint not null,
    version text not null,
    accepted_at timestamptz not null default now(),
    primary key (user_id, version)
);

create table if not exists orders (
    id text primary key,
    user_id bigint not null,
    sku text not null references products(sku),
    title text not null,
    price_stars integer not null,
    terms_version text not null,
    state text not null check (state in (
        'invoice', 'checkout', 'expired', 'cancelled', 'paid',
        'delivering', 'delivered', 'delivery_failed',
        'needs_refund', 'refund_pending', 'refunded'
    )),
    charge_id text unique,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    expires_at timestamptz not null default (now() + interval '15 minutes'),
    error_code text
);
create index if not exists orders_user on orders (user_id, created_at desc);

create table if not exists stock (
    id bigint generated always as identity primary key,
    sku text not null references products(sku),
    fingerprint text not null unique,
    ciphertext text not null,
    state text not null check (state in ('available', 'reserved', 'sold', 'quarantined')),
    order_id text unique references orders(id),
    created_at timestamptz not null default now()
);
create index if not exists stock_available on stock (sku, state, id);

create table if not exists payment_events (
    id text primary key,
    charge_id text not null unique,
    user_id bigint not null,
    payload text not null,
    currency text not null,
    amount integer not null,
    order_id text references orders(id),
    status text not null check (status in ('accepted', 'review', 'refund_pending', 'refunded')),
    reason text,
    created_at timestamptz not null default now()
);

create table if not exists refund_receipts (
    charge_id text primary key,
    user_id bigint not null,
    currency text not null,
    amount integer not null,
    created_at timestamptz not null default now()
);

-- Webhook dedup: Telegram retries deliveries; claim each update_id once.
create table if not exists update_inbox (
    update_id bigint primary key,
    kind text not null,
    created_at timestamptz not null default now()
);

-- Supplier (Canboso) tables — used by the fulfillment pipeline.
create table if not exists supplier_mappings (
    sku text primary key references products(sku),
    specification text not null
);

create table if not exists supplier_cache (
    name text primary key,
    key_hash text not null,
    ciphertext text not null,
    fetched_at timestamptz not null default now()
);

create table if not exists supplier_intents (
    order_id text primary key references orders(id),
    idempotency_key text not null unique,
    request_ciphertext text not null,
    key_hash text not null,
    product_id text not null,
    product_type text not null,
    max_cost text not null,
    currency text not null,
    state text not null default 'draft',
    response_ciphertext text not null default '',
    delivery_ciphertext text not null default '',
    supplier_reference text not null default '',
    actual_cost text,
    hold_reason text,
    attempts integer not null default 0,
    first_sent_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    resolution_version integer not null default 0
);

create table if not exists supplier_audit (
    id bigint generated always as identity primary key,
    order_id text not null references orders(id),
    operator_id text not null,
    action text not null,
    evidence_ciphertext text not null,
    created_at timestamptz not null default now()
);

-- Atomic webhook dedup. Returns true the first time an update_id is seen.
create or replace function claim_update(p_update_id bigint, p_kind text)
returns boolean
language plpgsql
as $$
declare
    n integer;
begin
    insert into update_inbox (update_id, kind) values (p_update_id, p_kind)
    on conflict (update_id) do nothing;
    get diagnostics n = row_count;
    return n > 0;
end;
$$;

-- Helper used by fulfill_order to detect supplier-sourced products.
create or replace function source_is_supplier(p_sku text)
returns boolean
language sql
stable
as $$
    select exists(select 1 from products where sku = p_sku and source = 'supplier');
$$;

-- Atomic payment + stock allocation. One transaction: records the payment,
-- takes one available stock unit (skip locked), marks the order delivered,
-- and returns the encrypted payload for the Worker to decrypt and deliver.
-- Idempotent on charge_id / delivered orders.
create or replace function fulfill_order(
    p_order_id text,
    p_charge_id text,
    p_user_id bigint,
    p_amount integer
)
returns json
language plpgsql
as $$
declare
    o orders%rowtype;
    s stock%rowtype;
begin
    select * into o from orders where id = p_order_id for update;
    if not found then
        return json_build_object('ok', false, 'reason', 'order_not_found');
    end if;
    if o.state = 'delivered' then
        return json_build_object('ok', true, 'duplicate', true);
    end if;
    if o.state not in ('invoice', 'checkout', 'paid') then
        return json_build_object('ok', false, 'reason', 'bad_state', 'state', o.state);
    end if;

    insert into payment_events (id, charge_id, user_id, payload, currency, amount, order_id, status)
    values ('pay_' || p_charge_id, p_charge_id, p_user_id, p_order_id, 'XTR', p_amount, p_order_id, 'accepted')
    on conflict (charge_id) do nothing;

    if source_is_supplier(o.sku) then
        -- supplier-sourced: paid, awaiting fulfillment pipeline
        update orders set state = 'delivering', charge_id = p_charge_id, updated_at = now()
        where id = p_order_id;
        return json_build_object('ok', true, 'duplicate', false, 'supplier', true, 'title', o.title);
    end if;

    update stock s2 set state = 'sold', order_id = p_order_id
    where s2.id = (
        select id from stock
        where sku = o.sku and state = 'available'
        order by id limit 1
        for update skip locked
    )
    returning * into s;

    if not found then
        update orders set state = 'needs_refund', charge_id = p_charge_id, updated_at = now()
        where id = p_order_id;
        return json_build_object('ok', false, 'reason', 'out_of_stock');
    end if;

    update orders set state = 'delivered', charge_id = p_charge_id, updated_at = now()
    where id = p_order_id;
    return json_build_object('ok', true, 'duplicate', false, 'ciphertext', s.ciphertext, 'title', o.title);
end;
$$;

-- Atomic refund: mark payment refunded, quarantine the sold code so it is
-- never resold, and record a receipt. Idempotent on charge_id.
create or replace function record_refund(
    p_charge_id text,
    p_user_id bigint,
    p_amount integer
)
returns json
language plpgsql
as $$
declare
    o orders%rowtype;
begin
    insert into refund_receipts (charge_id, user_id, currency, amount)
    values (p_charge_id, p_user_id, 'XTR', p_amount)
    on conflict (charge_id) do nothing;

    update payment_events set status = 'refunded' where charge_id = p_charge_id;

    select * into o from orders where charge_id = p_charge_id;
    if found then
        update orders set state = 'refunded', updated_at = now() where id = o.id;
        update stock set state = 'quarantined' where order_id = o.id;
    end if;
    return json_build_object('ok', true, 'order_id', o.id);
end;
$$;

-- Lock down: no anon/authenticated access. The Worker uses the service role key.
alter table metadata enable row level security;
alter table products enable row level security;
alter table terms_acceptances enable row level security;
alter table orders enable row level security;
alter table stock enable row level security;
alter table payment_events enable row level security;
alter table refund_receipts enable row level security;
alter table update_inbox enable row level security;
alter table supplier_mappings enable row level security;
alter table supplier_cache enable row level security;
alter table supplier_intents enable row level security;
alter table supplier_audit enable row level security;
