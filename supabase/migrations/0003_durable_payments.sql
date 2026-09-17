-- 0003: Durable webhook processing, pre-send stock allocation and
-- payment/refund invariants. Additive only; existing rows are preserved.

-- 1. Durable update inbox: claim BEFORE processing, lease, terminal states.
alter table update_inbox add column if not exists state text not null default 'processing';
alter table update_inbox add column if not exists attempts integer not null default 1;
alter table update_inbox add column if not exists last_error text;
alter table update_inbox add column if not exists updated_at timestamptz not null default now();

-- Rows claimed by the old code were fully processed (claim happened last).
update update_inbox set state = 'done' where state = 'processing';

alter table update_inbox drop constraint if exists update_inbox_state_check;
alter table update_inbox add constraint update_inbox_state_check
    check (state in ('processing', 'done', 'failed'));

-- claim_update returns:
--   'claimed'  first time we see this update -> process it
--   'retry'    previous attempt crashed and its lease went stale -> process again
--   'done'     fully processed -> acknowledge without reprocessing
--   'busy'     another invocation holds a fresh lease -> retryable failure
create or replace function claim_update(p_update_id bigint, p_kind text)
returns text
language plpgsql
as $$
declare
    st text;
begin
    insert into update_inbox (update_id, kind) values (p_update_id, p_kind)
    on conflict (update_id) do nothing;
    if found then
        return 'claimed';
    end if;
    select state into st from update_inbox where update_id = p_update_id;
    if st = 'done' then
        return 'done';
    end if;
    update update_inbox
       set state = 'processing', attempts = attempts + 1, updated_at = now()
     where update_id = p_update_id
       and state <> 'done'
       and updated_at < now() - interval '2 minutes';
    if found then
        return 'retry';
    end if;
    return 'busy';
end;
$$;

-- Record the outcome of a claimed update.
create or replace function finish_update(p_update_id bigint, p_ok boolean, p_error text default null)
returns void
language plpgsql
as $$
begin
    update update_inbox
       set state = case when p_ok then 'done' else 'failed' end,
           last_error = left(coalesce(p_error, ''), 300),
           updated_at = now()
     where update_id = p_update_id;
end;
$$;

-- 2. Delivery tracking on orders.
alter table orders add column if not exists delivered_at timestamptz;
alter table orders add column if not exists delivery_attempts integer not null default 0;

-- 3. Fulfillment: bind payer/amount/currency/charge to the order, allocate
--    stock before delivery, confirm delivery only after Telegram succeeds,
--    and persist unexpected paid events for review instead of dropping them.
create or replace function fulfill_order(
    p_order_id text,
    p_charge_id text,
    p_user_id bigint,
    p_amount integer,
    p_currency text
)
returns json
language plpgsql
as $$
declare
    o orders%rowtype;
    s stock%rowtype;
    existing payment_events%rowtype;
begin
    select * into o from orders where id = p_order_id for update;

    if not found then
        -- Paid event for an unknown order: keep it for review/refund.
        insert into payment_events (id, charge_id, user_id, payload, currency, amount, order_id, status, reason)
        values ('pay_' || p_charge_id, p_charge_id, p_user_id, p_order_id, p_currency, p_amount, null, 'review', 'order_not_found')
        on conflict (charge_id) do nothing;
        return json_build_object('ok', false, 'reason', 'order_not_found');
    end if;

    -- Replay of the charge already bound to this order: resend path only.
    if o.charge_id = p_charge_id and o.state in ('delivering', 'delivered') then
        return json_build_object('ok', true, 'duplicate', true, 'state', o.state);
    end if;

    select * into existing from payment_events where charge_id = p_charge_id;
    if found and existing.order_id is distinct from o.id then
        -- One charge must never fulfill two orders.
        return json_build_object('ok', false, 'reason', 'charge_conflict');
    end if;

    if o.state not in ('invoice', 'checkout', 'paid') then
        -- Late or unexpected payment on a closed order: retain for review.
        insert into payment_events (id, charge_id, user_id, payload, currency, amount, order_id, status, reason)
        values ('pay_' || p_charge_id, p_charge_id, p_user_id, p_order_id, p_currency, p_amount, o.id, 'review', 'bad_state:' || o.state)
        on conflict (charge_id) do nothing;
        return json_build_object('ok', false, 'reason', 'bad_state', 'state', o.state);
    end if;

    if o.user_id <> p_user_id or p_currency <> 'XTR' or o.price_stars <> p_amount then
        insert into payment_events (id, charge_id, user_id, payload, currency, amount, order_id, status, reason)
        values ('pay_' || p_charge_id, p_charge_id, p_user_id, p_order_id, p_currency, p_amount, o.id, 'review', 'payment_mismatch')
        on conflict (charge_id) do nothing;
        return json_build_object('ok', false, 'reason', 'payment_mismatch');
    end if;

    insert into payment_events (id, charge_id, user_id, payload, currency, amount, order_id, status)
    values ('pay_' || p_charge_id, p_charge_id, p_user_id, p_order_id, 'XTR', p_amount, o.id, 'accepted')
    on conflict (charge_id) do nothing;

    if source_is_supplier(o.sku) then
        -- Supplier fulfillment is not deployed: retain the payment for
        -- refund/review instead of an indefinite waiting state.
        update orders set state = 'needs_refund', charge_id = p_charge_id,
               error_code = 'supplier_unavailable', updated_at = now()
         where id = p_order_id;
        return json_build_object('ok', false, 'reason', 'supplier_unavailable');
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
        -- Stock disappeared after checkout approval: keep the payment and a
        -- clear refund state.
        update orders set state = 'needs_refund', charge_id = p_charge_id, updated_at = now()
         where id = p_order_id;
        return json_build_object('ok', false, 'reason', 'out_of_stock');
    end if;

    -- The assignment is durable BEFORE any Telegram send. 'delivered' is set
    -- only by confirm_delivery after Telegram confirms the message.
    update orders set state = 'delivering', charge_id = p_charge_id, updated_at = now()
     where id = p_order_id;
    return json_build_object('ok', true, 'duplicate', false, 'ciphertext', s.ciphertext, 'title', o.title);
end;
$$;

-- Mark delivery confirmed after Telegram accepted the send.
create or replace function confirm_delivery(p_order_id text)
returns void
language plpgsql
as $$
begin
    update orders set state = 'delivered', delivered_at = now(), updated_at = now()
     where id = p_order_id and state in ('delivering', 'delivered');
end;
$$;

-- Record a failed/uncertain delivery attempt. The assigned stock is kept, so a
-- retry resends the SAME code and never allocates a new one.
create or replace function record_delivery_failure(p_order_id text, p_error text)
returns void
language plpgsql
as $$
begin
    update orders set state = 'delivery_failed', error_code = left(coalesce(p_error, ''), 200),
           delivery_attempts = delivery_attempts + 1, updated_at = now()
     where id = p_order_id and state in ('delivering', 'delivery_failed', 'delivered');
end;
$$;

-- 4. Server-side pre-checkout validation: payer, XTR currency, exact Stars
--    price, order state, expiry, terms version, product activation, stock.
create or replace function pre_checkout_validate(
    p_order_id text,
    p_user_id bigint,
    p_amount integer,
    p_currency text,
    p_terms_version text
)
returns json
language plpgsql
as $$
declare
    o orders%rowtype;
    p products%rowtype;
    n integer;
begin
    select * into o from orders where id = p_order_id;
    if not found or o.user_id <> p_user_id then
        return json_build_object('ok', false, 'reason', 'not_found');
    end if;
    if o.state not in ('invoice', 'checkout') then
        return json_build_object('ok', false, 'reason', 'not_payable');
    end if;
    if o.expires_at < now() then
        update orders set state = 'expired', updated_at = now()
         where id = o.id and state in ('invoice', 'checkout');
        return json_build_object('ok', false, 'reason', 'expired');
    end if;
    if p_currency <> 'XTR' or p_amount <> o.price_stars then
        return json_build_object('ok', false, 'reason', 'amount_mismatch');
    end if;
    if o.terms_version <> p_terms_version then
        return json_build_object('ok', false, 'reason', 'terms');
    end if;
    select * into p from products where sku = o.sku;
    if not found or not p.active then
        return json_build_object('ok', false, 'reason', 'inactive');
    end if;
    if p.source = 'supplier' then
        return json_build_object('ok', false, 'reason', 'supplier_unavailable');
    end if;
    select count(*) into n from stock where sku = o.sku and state = 'available';
    if n = 0 then
        return json_build_object('ok', false, 'reason', 'out_of_stock');
    end if;
    update orders set state = 'checkout', updated_at = now()
     where id = o.id and state = 'invoice';
    return json_build_object('ok', true);
end;
$$;

-- 5. Refunds: verify the original charge, payer and amount; quarantine the
--    sold code so it is never resold; keep unmatched refunds for review.
create or replace function record_refund(p_charge_id text, p_user_id bigint, p_amount integer)
returns json
language plpgsql
as $$
declare
    pe payment_events%rowtype;
    o orders%rowtype;
begin
    insert into refund_receipts (charge_id, user_id, currency, amount)
    values (p_charge_id, p_user_id, 'XTR', p_amount)
    on conflict (charge_id) do nothing;

    select * into pe from payment_events where charge_id = p_charge_id;
    if not found then
        -- Refund for a charge we never recorded: retained above, flagged here.
        return json_build_object('ok', true, 'matched', false);
    end if;

    if pe.user_id <> p_user_id or pe.amount <> p_amount then
        update payment_events set status = 'review', reason = 'refund_mismatch'
         where charge_id = p_charge_id;
        return json_build_object('ok', true, 'matched', false, 'reason', 'refund_mismatch');
    end if;

    update payment_events set status = 'refunded' where charge_id = p_charge_id;

    select * into o from orders where charge_id = p_charge_id;
    if found then
        update orders set state = 'refunded', updated_at = now() where id = o.id;
        update stock set state = 'quarantined' where order_id = o.id;
    end if;
    return json_build_object('ok', true, 'matched', true, 'order_id', o.id);
end;
$$;

-- 6. Checkout pause switch (managed via the dashboard Settings screen; the
--    metadata table is the single source of truth, never deploy config).
create or replace function checkout_paused()
returns boolean
language sql
stable
as $$
    select coalesce((select value from metadata where key = 'checkout_paused'), 'false') = 'true';
$$;

-- Backward-compatible 4-argument overload: the previous Worker release calls
-- fulfill_order without a currency. It delegates to the v2 implementation so
-- live payments keep working between the migration and the Worker deploy.
create or replace function fulfill_order(
    p_order_id text,
    p_charge_id text,
    p_user_id bigint,
    p_amount integer
)
returns json
language sql
as $$
    select fulfill_order(p_order_id, p_charge_id, p_user_id, p_amount, 'XTR');
$$;
