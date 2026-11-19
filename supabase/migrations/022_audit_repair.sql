alter type public.job_kind add value if not exists 'repair';

alter table public.audit_events drop constraint if exists audit_events_kind_check;
alter table public.audit_events add constraint audit_events_kind_check
  check (kind in ('route', 'check', 'manager', 'repair'));

alter table public.audit_events drop constraint if exists audit_events_result_check;
alter table public.audit_events add constraint audit_events_result_check
  check (result in ('gemessen', 'fehlgeschlagen', 'ausgesetzt', 'Schritt mit Daniel', 'offen',
                    'neu gestartet', 'Warnung'));
