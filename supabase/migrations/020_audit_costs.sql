-- What one audit cost: calls, euros and Claude tokens per source and service, summed over every request of the audit.
-- Context: the worker adds each paid request through add_audit_cost next to the monthly spend of add_source_cost;
-- the app and the report show the rows. Claude runs on the subscription and has no price, only tokens.
-- Input: migrations 001-019. | Output: table audit_costs, function add_audit_cost (service role only).
-- Every statement is guarded, so running the file twice changes nothing.

create table if not exists public.audit_costs (
  audit_id uuid not null references public.audits(id) on delete cascade,
  source text not null,
  service text not null,
  calls integer not null default 0 check (calls >= 0),
  cost numeric not null default 0 check (cost >= 0),
  tokens_in bigint not null default 0 check (tokens_in >= 0),
  tokens_out bigint not null default 0 check (tokens_out >= 0),
  updated_at timestamptz not null default now(),
  primary key (audit_id, source, service)
);

comment on table public.audit_costs is
  'Calls, cost in euros and Claude tokens of one audit per source and service.';

alter table public.audit_costs enable row level security;

drop policy if exists reader_select on public.audit_costs;
create policy reader_select on public.audit_costs for select to authenticated using (true);

revoke all on table public.audit_costs from anon;
revoke insert, update, delete, truncate on table public.audit_costs from authenticated;
grant select on table public.audit_costs to authenticated;
grant all on table public.audit_costs to service_role;

create or replace function public.add_audit_cost(
  p_audit_id uuid, p_source text, p_service text, p_calls integer, p_cost numeric,
  p_tokens_in bigint, p_tokens_out bigint
) returns void
language sql set search_path = public, pg_temp as $$
  insert into public.audit_costs as c (audit_id, source, service, calls, cost, tokens_in, tokens_out)
  values (p_audit_id, p_source, p_service, p_calls, p_cost, p_tokens_in, p_tokens_out)
  on conflict (audit_id, source, service) do update set
    calls = c.calls + excluded.calls,
    cost = c.cost + excluded.cost,
    tokens_in = c.tokens_in + excluded.tokens_in,
    tokens_out = c.tokens_out + excluded.tokens_out,
    updated_at = now();
$$;

revoke all on function public.add_audit_cost(uuid, text, text, integer, numeric, bigint, bigint) from public, anon, authenticated;
grant execute on function public.add_audit_cost(uuid, text, text, integer, numeric, bigint, bigint) to service_role;