alter type public.check_outcome add value if not exists 'not_applicable';

alter table public.audit_events add column if not exists protocol jsonb;

alter table public.audit_events drop constraint if exists audit_events_protocol_check;
alter table public.audit_events add constraint audit_events_protocol_check
  check (protocol is null or jsonb_typeof(protocol) = 'object');

alter table public.audit_events drop constraint if exists audit_events_kind_check;
alter table public.audit_events add constraint audit_events_kind_check
  check (kind in ('route', 'check', 'manager', 'repair', 'measurement'));

alter table public.audit_events drop constraint if exists audit_events_result_check;
alter table public.audit_events add constraint audit_events_result_check
  check (result in ('gemessen', 'fehlgeschlagen', 'ausgesetzt', 'Schritt mit Daniel', 'offen',
                    'neu gestartet', 'Warnung', 'läuft', 'gültig', 'ungültig'));

alter table public.check_results add column if not exists provider text;
alter table public.check_results add column if not exists verdict_collected_ok boolean;
alter table public.check_results add column if not exists verdict_collected_reason text;
alter table public.check_results add column if not exists verdict_collected_evidence text;
alter table public.check_results add column if not exists verdict_data_ok boolean;
alter table public.check_results add column if not exists verdict_data_reason text;
alter table public.check_results add column if not exists verdict_data_evidence text;
alter table public.check_results add column if not exists verdict_evaluation_ok boolean;
alter table public.check_results add column if not exists verdict_evaluation_reason text;
alter table public.check_results add column if not exists verdict_evaluation_evidence text;
alter table public.check_results add column if not exists control_state text not null default 'open';
alter table public.check_results add column if not exists control_attempt integer not null default 0;
alter table public.check_results add column if not exists control_last_error text;

alter table public.check_results drop constraint if exists check_results_provider_check;
alter table public.check_results add constraint check_results_provider_check
  check (provider is null or provider ~ '^[a-z][a-z_]*$');

alter table public.check_results drop constraint if exists check_results_control_state_check;
alter table public.check_results add constraint check_results_control_state_check
  check (control_state in ('open', 'done'));

alter table public.check_results drop constraint if exists check_results_control_attempt_check;
alter table public.check_results add constraint check_results_control_attempt_check
  check (control_attempt >= 0);

alter table public.check_results drop constraint if exists check_results_verdict_collected_check;
alter table public.check_results add constraint check_results_verdict_collected_check
  check ((verdict_collected_ok is not true or nullif(btrim(verdict_collected_evidence), '') is not null)
     and (verdict_collected_ok is not false or nullif(btrim(verdict_collected_reason), '') is not null));

alter table public.check_results drop constraint if exists check_results_verdict_data_check;
alter table public.check_results add constraint check_results_verdict_data_check
  check ((verdict_data_ok is not true or nullif(btrim(verdict_data_evidence), '') is not null)
     and (verdict_data_ok is not false or nullif(btrim(verdict_data_reason), '') is not null));

alter table public.check_results drop constraint if exists check_results_verdict_evaluation_check;
alter table public.check_results add constraint check_results_verdict_evaluation_check
  check ((verdict_evaluation_ok is not true or nullif(btrim(verdict_evaluation_evidence), '') is not null)
     and (verdict_evaluation_ok is not false or nullif(btrim(verdict_evaluation_reason), '') is not null));

alter table public.check_results drop constraint if exists check_results_control_done_check;
alter table public.check_results add constraint check_results_control_done_check
  check (control_state <> 'done'
         or (verdict_collected_ok is not null
             and verdict_data_ok is not null
             and verdict_evaluation_ok is not null));
