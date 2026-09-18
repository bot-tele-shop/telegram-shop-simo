-- 0005: Earnings analytics for the dashboard.
-- Read-only aggregation over payment_events (money truth) and orders
-- (per-product breakdown). Service-role only, like the rest of admin.

create or replace function admin_analytics()
returns json
language plpgsql
stable
as $$
begin
    return json_build_object(
        -- Money actually received vs returned, from payment_events.
        -- 'accepted' is money currently kept, 'refund_pending' is a refund in
        -- flight, 'refunded' is money returned to the buyer, 'review' is
        -- received but flagged (wrong amount / unknown order / replay).
        'revenue', json_build_object(
            'kept_stars', coalesce((
                select sum(amount)::int from payment_events
                where status = 'accepted'
            ), 0),
            'pending_refund_stars', coalesce((
                select sum(amount)::int from payment_events
                where status = 'refund_pending'
            ), 0),
            'refunded_stars', coalesce((
                select sum(amount)::int from payment_events
                where status = 'refunded'
            ), 0),
            'review_stars', coalesce((
                select sum(amount)::int from payment_events
                where status = 'review'
            ), 0),
            'payments_count', (
                select count(*)::int from payment_events
                where status in ('accepted', 'refund_pending', 'refunded')
            ),
            'first_payment_at', (
                select min(created_at) from payment_events
                where status in ('accepted', 'refund_pending', 'refunded')
            )
        ),

        -- Per-product sales from orders. A sale counts once money was
        -- received (paid or beyond); refunded orders are broken out.
        'by_product', coalesce((
            select json_agg(row_to_json(t) order by t.net_stars desc)
            from (
                select
                    o.sku,
                    o.title,
                    count(*) filter (
                        where o.state in ('paid', 'delivering', 'delivered',
                                          'delivery_failed', 'needs_refund',
                                          'refund_pending')
                    )::int as sales,
                    coalesce(sum(o.price_stars) filter (
                        where o.state in ('paid', 'delivering', 'delivered',
                                          'delivery_failed', 'needs_refund',
                                          'refund_pending')
                    ), 0)::int as earned_stars,
                    count(*) filter (where o.state = 'refunded')::int as refunds,
                    coalesce(sum(o.price_stars) filter (
                        where o.state = 'refunded'
                    ), 0)::int as refunded_stars,
                    (coalesce(sum(o.price_stars) filter (
                        where o.state in ('paid', 'delivering', 'delivered',
                                          'delivery_failed', 'needs_refund',
                                          'refund_pending')
                    ), 0))::int as net_stars
                from orders o
                where o.state not in ('invoice', 'checkout', 'expired', 'cancelled')
                group by o.sku, o.title
            ) t
        ), '[]'::json),

        -- Stars received per day for the last 14 days (chart data).
        'by_day', coalesce((
            select json_agg(row_to_json(t) order by t.day)
            from (
                select
                    d.day,
                    coalesce(sum(pe.amount) filter (
                        where pe.status in ('accepted', 'refund_pending')
                    ), 0)::int as earned_stars,
                    coalesce(sum(pe.amount) filter (
                        where pe.status = 'refunded'
                    ), 0)::int as refunded_stars
                from (
                    select generate_series(
                        date_trunc('day', now()) - interval '13 days',
                        date_trunc('day', now()),
                        interval '1 day'
                    ) as day
                ) d
                left join payment_events pe
                    on date_trunc('day', pe.created_at) = d.day
                group by d.day
            ) t
        ), '[]'::json)
    );
end;
$$;
