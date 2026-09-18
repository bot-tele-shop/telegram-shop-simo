-- 0006: Keep update payloads so failed updates can be inspected and retried
-- from the owner dashboard instead of the SQL editor.
-- Payloads remain service-role only; RLS and grants are unchanged.

alter table update_inbox add column if not exists payload jsonb;

-- claim_update gains the payload. Retry claims refresh it so a redelivery
-- always re-runs the newest body Telegram sent.
create or replace function claim_update(p_update_id bigint, p_kind text, p_payload jsonb default null)
returns text
language plpgsql
as $$
declare
    st text;
begin
    insert into update_inbox (update_id, kind, payload) values (p_update_id, p_kind, p_payload)
    on conflict (update_id) do nothing;
    if found then
        return 'claimed';
    end if;
    select state into st from update_inbox where update_id = p_update_id;
    if st = 'done' then
        return 'done';
    end if;
    update update_inbox
       set state = 'processing', attempts = attempts + 1, updated_at = now(),
           payload = coalesce(p_payload, payload)
     where update_id = p_update_id
       and state <> 'done'
       and updated_at < now() - interval '2 minutes';
    if found then
        return 'retry';
    end if;
    return 'busy';
end;
$$;

-- Dashboard retry: only a failed update with a stored payload can be re-armed.
-- The backdated updated_at lets the very next claim take the 'retry' path.
create or replace function rearm_failed_update(p_update_id bigint)
returns boolean
language plpgsql
as $$
begin
    update update_inbox
       set state = 'processing', updated_at = now() - interval '10 minutes', last_error = null
     where update_id = p_update_id
       and state = 'failed'
       and payload is not null;
    return found;
end;
$$;
