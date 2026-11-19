-- Cost estimate before an audit starts, the verdict on it, and the social media measurement of the audit.
-- Context: the collector prices the planned requests from the real quantities (keywords, questions times repeats, competitors, social accounts) and the unit prices, compares them with the remaining credit of DataForSEO and Apify and the monthly caps, and stores the result here; the app shows it before and next to the real costs. An audit whose estimate is not covered does not start.
-- Input: migrations 001-020. | Output: table audit_estimates, columns audits.estimate_status, audits.estimate_note, audits.social.
-- Every statement is guarded, so running the file twice changes nothing.

create table if not exists public.audit_estimates (
  audit_id uuid not null references public.audits(id) on delete cascade,
  source text not null,
  service text not null,
  calls integer not null default 0 check (calls >= 0),
  unit_cost numeric not null default 0 check (unit_cost >= 0),
  cost numeric not null default 0 check (cost >= 0),
  created_at timestamptz not null default now(),
  primary key (audit_id, source, service)
);

comment on table public.audit_estimates is
  'Planned calls, unit price and cost in euros of one audit per source and service, computed before the first paid request.';

alter table public.audit_estimates enable row level security;

drop policy if exists reader_select on public.audit_estimates;
create policy reader_select on public.audit_estimates for select to authenticated using (true);

revoke all on table public.audit_estimates from anon;
revoke insert, update, delete, truncate on table public.audit_estimates from authenticated;
grant select on table public.audit_estimates to authenticated;
grant all on table public.audit_estimates to service_role;

alter table public.audits add column if not exists estimate_status text
  check (estimate_status in ('covered', 'insufficient', 'unchecked'));
alter table public.audits add column if not exists estimate_note text;
alter table public.audits add column if not exists social jsonb;

comment on column public.audits.estimate_status is
  'covered: the credit and caps cover the estimate; insufficient: the audit did not start; unchecked: a balance could not be read.';
comment on column public.audits.estimate_note is 'Plain-language reason for the estimate status.';
comment on column public.audits.social is
  'Social media measurement of the audit: {accounts: [...], gaps: [...]}, the same shape as the social key of the raw data.';