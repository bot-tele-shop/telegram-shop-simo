-- 0004: Private admin dashboard support.
-- Audit log for owner actions plus read-model functions for the dashboard.
-- Everything remains service-role only (RLS enabled, no anon grants).

create table if not exists admin_audit (
    id bigint generated always as identity primary key,
    actor text not null,
    action text not null,
    target text,
    detail jsonb not null default '{}',
    created_at timestamptz not null default now()
);
alter table admin_audit enable row level security;

-- Overview counts for the dashboard home screen.
create or replace function admin_overview()
returns json
language plpgsql
stable
as $$
begin
    return json_build_object(
        'products', json_build_object(
            'total', (select count(*)::int from products),
            'active', (select count(*)::int from products where active)
        ),
        'stock', json_build_object(
            'available', (select count(*)::int from stock where state = 'available'),
            'sold', (select count(*)::int from stock where state = 'sold'),
            'reserved', (select count(*)::int from stock where state = 'reserved'),
            'quarantined', (select count(*)::int from stock where state = 'quarantined')
        ),
        'orders', json_build_object(
            'total', (select count(*)::int from orders),
            'paid', (select count(*)::int from orders where state in ('delivering', 'delivered')),
            'pending_delivery', (select count(*)::int from orders where state in ('delivering', 'delivery_failed')),
            'needs_refund', (select count(*)::int from orders where state in ('needs_refund', 'refund_pending')),
            'open_invoices', (select count(*)::int from orders where state in ('invoice', 'checkout'))
        ),
        'review_alerts', (select count(*)::int from payment_events where status = 'review'),
        'failed_updates', (select count(*)::int from update_inbox where state = 'failed'),
        'checkout_paused', (select coalesce((select value from metadata where key = 'checkout_paused'), 'false') = 'true')
    );
end;
$$;

-- Products with stock counts for the Products screen (includes inactive).
create or replace function admin_products()
returns json
language sql
stable
as $$
    select coalesce(json_agg(row_to_json(t) order by t.sku), '[]'::json)
    from (
        select
            p.sku, p.title, p.description, p.category, p.price_stars, p.active,
            p.source, p.is_demo,
            (select count(*)::int from stock s where s.sku = p.sku and s.state = 'available') as available,
            (select count(*)::int from stock s where s.sku = p.sku and s.state = 'sold') as sold,
            (select count(*)::int from stock s where s.sku = p.sku and s.state = 'quarantined') as quarantined
        from products p
    ) t;
$$;

-- Orders joined with payment status for the Orders screen.
create or replace function admin_orders(p_state text default null, p_query text default null, p_limit integer default 100)
returns json
language plpgsql
stable
as $$
begin
    return (
        select coalesce(json_agg(row_to_json(t) order by t.created_at desc), '[]'::json)
        from (
            select
                o.id, o.user_id, o.sku, o.title, o.price_stars, o.state,
                o.charge_id, o.created_at, o.updated_at, o.delivered_at,
                o.delivery_attempts, o.error_code,
                pe.status as payment_status
            from orders o
            left join payment_events pe on pe.order_id = o.id
            where (p_state is null or o.state = p_state)
              and (p_query is null or o.id ilike '%' || p_query || '%'
                   or o.title ilike '%' || p_query || '%'
                   or o.user_id::text = p_query)
            limit least(p_limit, 200)
        ) t
    );
end;
$$;
