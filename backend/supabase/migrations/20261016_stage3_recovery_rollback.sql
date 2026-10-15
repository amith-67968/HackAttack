-- Roll back the Stage 3 recovery migration. Apply only when intentionally removing Stage 3 recovery.
drop trigger if exists research_task_transition_guard on public.research_tasks;
drop trigger if exists research_step_transition_guard on public.research_steps;
drop function if exists public.enforce_research_task_transition();
drop function if exists public.enforce_research_step_transition();
drop function if exists public.request_research_cancellation(uuid);
drop function if exists public.release_research_task(uuid, uuid);
drop function if exists public.heartbeat_research_task(uuid, uuid);
drop function if exists public.claim_research_task(uuid, uuid, boolean);
drop function if exists public.append_research_task_event(uuid, text, jsonb);
drop table if exists public.research_step_attempts;
drop table if exists public.research_task_events;
drop table if exists public.task_event_counters;
alter table public.research_steps drop column if exists last_error_classification;
alter table public.sources drop column if exists page_content;
alter table public.research_tasks drop column if exists lease_expires_at;
alter table public.research_tasks drop column if exists worker_id;
alter table public.research_tasks drop column if exists cancel_requested;
