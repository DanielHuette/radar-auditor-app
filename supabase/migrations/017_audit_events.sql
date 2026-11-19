-- Feedback after every measurement (A4): one row per event of a measuring route or of a check.
-- Context: every measuring route (website, PageSpeed, Google Maps, Google search, Bing, Google Images,
-- directories, every AI assistant, DataForSEO, customer access) reports an event when it ends, and after
-- scoring every check does. The worker writes them with the service key; the app reads them. An audit
-- whose measuring route of a critical stage failed without a fallback stops with the new status halted
-- and waits for Daniel. | Input: migrations 001-016. | Output: audit status halted, table audit_events
-- with realtime.
-- Every statement is guarded, so running the file twice changes nothing. The new enum value cannot be
-- used in the transaction that adds it; no statement here uses it.

alter type public.audit_status add value if not exists 'halted';

create table if not exists public.audit_events (
  id uuid primary key default gen_random_uuid(),
  audit_id uuid not null references public.audits(id) on delete cascade,
  occurred_at timestamptz not null default now(),
  kind text not null check (kind in ('route', 'check')),
  phase text not null check (phase in ('vorab', 'messung', 'auswertung')),
  result text not null check (result in ('gemessen', 'fehlgeschlagen', 'ausgesetzt', 'Schritt mit Daniel', 'offen')),
  reason text,
  stage integer check (stage is null or stage between 0 and 4),
  route text,
  label text not null,
  check_number integer check (check_number is null or check_number >= 1),
  check_id text,
  checks jsonb not null default '[]' check (jsonb_typeof(checks) = 'array')
);
create index if not exists audit_events_audit on public.audit_events (audit_id, occurred_at);
create index if not exists audit_events_failed on public.audit_events (audit_id)
  where result = 'fehlgeschlagen';

comment on table public.audit_events is
  'Feedback after every measurement: one row per measuring route and, after scoring, per check.';

alter table public.audit_events enable row level security;

drop policy if exists reader_select on public.audit_events;
create policy reader_select on public.audit_events for select to authenticated using (true);

revoke all on table public.audit_events from anon;
revoke insert, update, delete, truncate on table public.audit_events from authenticated;
grant select on table public.audit_events to authenticated;
grant all on table public.audit_events to service_role;

do $$
declare
  v_publication oid;
  v_table regclass := to_regclass('public.audit_events');
begin
  select oid into v_publication from pg_publication where pubname = 'supabase_realtime';
  if v_publication is null or v_table is null then
    return;
  end if;
  if not exists (select 1 from pg_publication_rel where prpubid = v_publication and prrelid = v_table) then
    execute format('alter publication supabase_realtime add table %s', v_table);
  end if;
end $$;
