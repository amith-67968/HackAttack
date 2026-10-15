-- ProofLoop Stage 3 durability, retries, resumability and event replay.
-- Rollback: if 20261017_stage3_resume_claim.sql was applied, run its rollback
-- first. Then apply 20261016_stage3_recovery_rollback.sql. Stage 3 attempt,
-- event and page-content data will be removed by rollback.

alter table public.research_tasks
    add column if not exists cancel_requested boolean not null default false,
    add column if not exists worker_id uuid,
    add column if not exists lease_expires_at timestamptz;
alter table public.research_steps add column if not exists last_error_classification text;
alter table public.sources add column if not exists page_content text;

create table if not exists public.task_event_counters (
    task_id uuid primary key references public.research_tasks(id) on delete cascade,
    last_sequence bigint not null default 0
);

create table if not exists public.research_task_events (
    id uuid primary key default gen_random_uuid(),
    task_id uuid not null references public.research_tasks(id) on delete cascade,
    sequence_no bigint not null,
    event_type text not null,
    payload jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    unique (task_id, sequence_no)
);

create table if not exists public.research_step_attempts (
    id uuid primary key default gen_random_uuid(),
    task_id uuid not null references public.research_tasks(id) on delete cascade,
    step_id uuid not null references public.research_steps(id) on delete cascade,
    attempt_no integer not null check (attempt_no > 0),
    tool_name text not null,
    status text not null check (status in ('RUNNING','SUCCEEDED','FAILED','INTERRUPTED')),
    error_classification text,
    safe_error text,
    started_at timestamptz not null default now(),
    completed_at timestamptz,
    unique (step_id, attempt_no)
);

create index if not exists research_task_events_cursor_idx
    on public.research_task_events(task_id, sequence_no);
create index if not exists research_step_attempts_task_idx
    on public.research_step_attempts(task_id, step_id, attempt_no);

alter table public.task_event_counters enable row level security;
alter table public.research_task_events enable row level security;
alter table public.research_step_attempts enable row level security;

drop policy if exists task_events_owner_read on public.research_task_events;
create policy task_events_owner_read on public.research_task_events for select to authenticated
    using (exists (select 1 from public.research_tasks t where t.id = task_id and t.user_id = (select auth.uid())));
drop policy if exists step_attempts_owner_read on public.research_step_attempts;
create policy step_attempts_owner_read on public.research_step_attempts for select to authenticated
    using (exists (select 1 from public.research_tasks t where t.id = task_id and t.user_id = (select auth.uid())));
-- The counter table is internal to the SECURITY DEFINER event function.
revoke all on public.task_event_counters from anon, authenticated;
grant select on public.research_task_events, public.research_step_attempts to authenticated;
grant select, insert, update, delete on public.research_task_events, public.research_step_attempts to service_role;

create or replace function public.append_research_task_event(p_task_id uuid, p_event_type text, p_payload jsonb)
returns table(id uuid, sequence_no bigint, created_at timestamptz)
language plpgsql security definer set search_path = public, pg_temp as $$
declare
    next_sequence bigint;
    event_id uuid;
    event_created_at timestamptz;
begin
    insert into public.task_event_counters(task_id, last_sequence) values (p_task_id, 1)
    on conflict (task_id) do update set last_sequence = task_event_counters.last_sequence + 1
    returning last_sequence into next_sequence;
    insert into public.research_task_events(task_id, sequence_no, event_type, payload)
    values (p_task_id, next_sequence, p_event_type, coalesce(p_payload, '{}'::jsonb))
    returning research_task_events.id, research_task_events.created_at into event_id, event_created_at;
    id := event_id;
    sequence_no := next_sequence;
    created_at := event_created_at;
    return next;
end;
$$;
revoke all on function public.append_research_task_event(uuid, text, jsonb) from public, anon, authenticated;
grant execute on function public.append_research_task_event(uuid, text, jsonb) to service_role;

create or replace function public.claim_research_task(p_task_id uuid, p_worker_id uuid, p_resume boolean)
returns boolean
language plpgsql security definer set search_path = public, pg_temp as $$
declare claimed boolean := false;
begin
    if p_resume then
        update public.research_tasks t
           set status = 'RECOVERING', worker_id = p_worker_id,
               lease_expires_at = now() + interval '5 minutes', cancel_requested = false,
               completed_at = null, updated_at = now()
         where t.id = p_task_id
           and t.status in ('QUEUED','PLANNING','FAILED','COMPLETED_WITH_LIMITATIONS','RUNNING','RECOVERING')
           and (t.worker_id is null or t.lease_expires_at is null or t.lease_expires_at < now())
           and (
               exists (select 1 from public.research_steps s where s.task_id = t.id
                       and (s.status in ('PENDING','RUNNING','RETRYING') or
                            (s.status = 'FAILED' and coalesce(s.last_error_classification, 'unknown') not in
                             ('authorization','validation'))))
               or exists (select 1 from public.sources src where src.task_id = t.id and src.fetch_status in ('discovered','retrieved','fetch_failed','extraction_failed'))
               or (t.status in ('RUNNING','RECOVERING') and t.plan is not null)
               or (t.status in ('QUEUED','PLANNING') and t.plan is null)
           )
        returning true into claimed;
        if coalesce(claimed, false) then
            update public.research_steps set status = 'RETRYING', updated_at = now()
             where task_id = p_task_id and status = 'RUNNING';
            update public.research_step_attempts set status = 'INTERRUPTED', completed_at = now(),
                   error_classification = 'interrupted', safe_error = 'Worker stopped before recording an outcome.'
             where task_id = p_task_id and status = 'RUNNING';
        end if;
    else
        update public.research_tasks t
           set status = 'PLANNING', worker_id = p_worker_id,
               lease_expires_at = now() + interval '5 minutes', updated_at = now()
         where t.id = p_task_id and t.status = 'QUEUED'
        returning true into claimed;
    end if;
    return coalesce(claimed, false);
end;
$$;
revoke all on function public.claim_research_task(uuid, uuid, boolean) from public, anon, authenticated;
grant execute on function public.claim_research_task(uuid, uuid, boolean) to service_role;

create or replace function public.heartbeat_research_task(p_task_id uuid, p_worker_id uuid)
returns boolean language sql security definer set search_path = public, pg_temp as $$
    update public.research_tasks set lease_expires_at = now() + interval '5 minutes', updated_at = now()
     where id = p_task_id and worker_id = p_worker_id and cancel_requested = false
    returning true;
$$;
revoke all on function public.heartbeat_research_task(uuid, uuid) from public, anon, authenticated;
grant execute on function public.heartbeat_research_task(uuid, uuid) to service_role;

create or replace function public.release_research_task(p_task_id uuid, p_worker_id uuid)
returns void language sql security definer set search_path = public, pg_temp as $$
    update public.research_tasks set worker_id = null, lease_expires_at = null, updated_at = now()
     where id = p_task_id and worker_id = p_worker_id;
$$;
revoke all on function public.release_research_task(uuid, uuid) from public, anon, authenticated;
grant execute on function public.release_research_task(uuid, uuid) to service_role;

create or replace function public.request_research_cancellation(p_task_id uuid)
returns boolean language plpgsql security definer set search_path = public, pg_temp as $$
declare changed boolean := false;
begin
    update public.research_tasks
       set cancel_requested = true,
           status = case when status = 'QUEUED' and worker_id is null then 'CANCELLED' else status end,
           completed_at = case when status = 'QUEUED' and worker_id is null then now() else completed_at end,
           updated_at = now()
     where id = p_task_id and status not in ('COMPLETED','COMPLETED_WITH_LIMITATIONS','FAILED','CANCELLED')
     returning true into changed;
    return coalesce(changed, false);
end;
$$;
revoke all on function public.request_research_cancellation(uuid) from public, anon, authenticated;
grant execute on function public.request_research_cancellation(uuid) to service_role;

create or replace function public.enforce_research_task_transition()
returns trigger language plpgsql as $$
begin
    if old.status is distinct from new.status and not (
        (old.status = 'QUEUED' and new.status in ('PLANNING','RECOVERING','CANCELLED','FAILED')) or
        (old.status = 'PLANNING' and new.status in ('RUNNING','RECOVERING','CANCELLED','FAILED')) or
        (old.status = 'RUNNING' and new.status in ('VERIFYING','RECOVERING','COMPLETED','COMPLETED_WITH_LIMITATIONS','FAILED','CANCELLED')) or
        (old.status = 'VERIFYING' and new.status in ('RUNNING','RECOVERING','COMPLETED','COMPLETED_WITH_LIMITATIONS','FAILED','CANCELLED')) or
        (old.status = 'RECOVERING' and new.status in ('PLANNING','RUNNING','COMPLETED','COMPLETED_WITH_LIMITATIONS','FAILED','CANCELLED')) or
        (old.status = 'FAILED' and new.status = 'RECOVERING') or
        (old.status = 'COMPLETED_WITH_LIMITATIONS' and new.status = 'RECOVERING')
    ) then raise exception 'Illegal research task status transition: % -> %', old.status, new.status; end if;
    if new.status in ('COMPLETED','COMPLETED_WITH_LIMITATIONS') and exists (
        select 1 from public.research_steps s where s.task_id = new.id and s.status in ('PENDING','RUNNING','RETRYING')
    ) then raise exception 'Cannot complete task while steps remain unresolved'; end if;
    return new;
end;
$$;
drop trigger if exists research_task_transition_guard on public.research_tasks;
create trigger research_task_transition_guard before update of status on public.research_tasks
    for each row execute function public.enforce_research_task_transition();

create or replace function public.enforce_research_step_transition()
returns trigger language plpgsql as $$
begin
    if old.status = 'VERIFIED' and new.status <> 'VERIFIED' then
        raise exception 'A verified research step is immutable';
    end if;
    if old.status is distinct from new.status and not (
        (old.status = 'PENDING' and new.status in ('RUNNING','SKIPPED')) or
        (old.status = 'RUNNING' and new.status in ('RETRYING','SUCCEEDED','VERIFIED','FAILED','SKIPPED')) or
        (old.status = 'RETRYING' and new.status in ('RUNNING','FAILED','SKIPPED')) or
        (old.status = 'FAILED' and new.status in ('RETRYING','RUNNING')) or
        (old.status = 'SUCCEEDED' and new.status = 'VERIFIED')
    ) then raise exception 'Illegal research step status transition: % -> %', old.status, new.status; end if;
    return new;
end;
$$;
drop trigger if exists research_step_transition_guard on public.research_steps;
create trigger research_step_transition_guard before update of status on public.research_steps
    for each row execute function public.enforce_research_step_transition();
