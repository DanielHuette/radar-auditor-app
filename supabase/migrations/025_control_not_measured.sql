alter table public.check_results drop constraint if exists check_results_control_done_check;
alter table public.check_results add constraint check_results_control_done_check
  check (control_state <> 'done'
         or (verdict_collected_ok is not null
             and (verdict_collected_ok is false
                  or outcome = 'not_measurable'
                  or (verdict_data_ok is not null and verdict_evaluation_ok is not null))));
