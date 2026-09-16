-- Catalog with per-product available stock counts in a single round trip.
create or replace function catalog_with_stock()
returns json
language sql
stable
as $$
    select coalesce(json_agg(row_to_json(t) order by t.sku), '[]'::json)
    from (
        select
            p.sku, p.title, p.description, p.category, p.price_stars, p.source,
            (select count(*)::int from stock s
             where s.sku = p.sku and s.state = 'available') as available
        from products p
        where p.active
    ) t;
$$;
