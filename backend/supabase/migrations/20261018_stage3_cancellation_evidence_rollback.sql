-- Roll back 20261018_stage3_cancellation_evidence.sql. Existing rows are preserved.
drop function if exists public.start_research_attempt(uuid, uuid, text, integer, text);
drop function if exists public.save_research_findings_if_active(uuid, uuid, jsonb);
drop function if exists public.finish_research_cancellation(uuid, uuid);
drop function if exists public.finalize_pending_research_cancellations();

-- Restore the cancellation and claim RPCs installed by Stage 3 before this migration.
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

create or replace function public.claim_research_task(p_task_id uuid, p_worker_id uuid, p_resume boolean)
returns boolean language plpgsql security definer set search_path = public, pg_temp as $$
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
                            (s.status = 'FAILED' and coalesce(s.last_error_classification, 'unknown') not in ('authorization','validation'))))
               or exists (select 1 from public.sources src where src.task_id = t.id
                          and src.fetch_status in ('discovered','retrieved','fetch_failed','extraction_failed'))
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
        update public.research_tasks t set status = 'PLANNING', worker_id = p_worker_id,
               lease_expires_at = now() + interval '5 minutes', updated_at = now()
         where t.id = p_task_id and t.status = 'QUEUED'
        returning true into claimed;
    end if;
    return coalesce(claimed, false);
end;
$$;
revoke all on function public.claim_research_task(uuid, uuid, boolean) from public, anon, authenticated;
grant execute on function public.claim_research_task(uuid, uuid, boolean) to service_role;

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
