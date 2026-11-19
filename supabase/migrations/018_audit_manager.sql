-- The audit manager (an agent that carries one audit from collecting to the report) writes its notes and its
-- verdict as audit_events of the kind manager, and takes the jobs of one audit one by one.
-- Input: migrations 001-017. | Output: kind manager in audit_events, function claim_audit_job.
-- Every statement is guarded, so running the file twice changes nothing.

alter table public.audit_events drop constraint if exists audit_events_kind_check;
alter table public.audit_events add constraint audit_events_kind_check check (kind in ('route', 'check', 'manager'));

create or replace function public.claim_audit_job(p_audit_id uuid, p_kinds public.job_kind[]) returns setof public.jobs
language sql set search_path = public, pg_temp as $$
  update public.jobs set status = 'running', attempts = attempts + 1, updated_at = now()
  where id in (
    select id from public.jobs
    where audit_id = p_audit_id and kind = any (p_kinds) and status = 'queued'
      and (run_after is null or run_after <= now())
    order by array_position(p_kinds, kind), created_at limit 1 for update skip locked
  )
  returning *;
$$;
revoke all on function public.claim_audit_job(uuid, public.job_kind[]) from public, anon, authenticated;
grant execute on function public.claim_audit_job(uuid, public.job_kind[]) to service_role;