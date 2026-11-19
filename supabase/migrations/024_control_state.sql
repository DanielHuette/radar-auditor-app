alter type public.audit_status add value if not exists 'controlling';

alter table public.check_results drop constraint if exists check_results_control_done_check;
alter table public.check_results add constraint check_results_control_done_check
  check (control_state <> 'done'
         or (verdict_collected_ok is not null
             and (verdict_collected_ok is false
                  or (verdict_data_ok is not null and verdict_evaluation_ok is not null))));

alter table public.audits drop constraint if exists audits_disabled_checks_ids;
alter table public.audits add constraint audits_disabled_checks_ids
  check (cardinality(disabled_checks) = 0
         or array_to_string(disabled_checks, ',', '~') ~ '^[a-z]\d+(,[a-z]\d+)*$');
