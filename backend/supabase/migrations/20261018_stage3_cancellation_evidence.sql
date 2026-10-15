-- Stage 3 cancellation boundaries and evidence-safe finding persistence.
-- Apply after 20261017_stage3_resume_claim.sql. Preserves existing task/finding rows.

create or replace function public.start_research_attempt(
    p_task_id uuid, p_worker_id uuid, p_action_key text, p_attempt_no integer, p_tool_name text
) returns jsonb
language plpgsql security definer set search_path = public, pg_temp as $$
declare
    task_row public.research_tasks%rowtype;
    step_row public.research_steps%rowtype;
    attempt_row public.research_step_attempts%rowtype;
begin
    select * into task_row from public.research_tasks where id = p_task_id for update;
    if not found or task_row.cancel_requested or task_row.worker_id is distinct from p_worker_id
       or task_row.status not in ('PLANNING','RUNNING','RECOVERING') then
        return null;
    end if;

    update public.research_steps as target_step
       set status = 'RUNNING', attempt_count = p_attempt_no,
           started_at = now(), completed_at = null, updated_at = now()
     where target_step.task_id = p_task_id and target_step.action_key = p_action_key
       and target_step.status in ('PENDING','RUNNING','RETRYING','FAILED')
       and not exists (
           select 1 from public.research_step_attempts active_attempt
            where active_attempt.step_id = target_step.id
              and active_attempt.status = 'RUNNING'
       )
     returning * into step_row;
    if not found then return null; end if;

    insert into public.research_step_attempts(task_id, step_id, attempt_no, tool_name, status, started_at)
    values (p_task_id, step_row.id, p_attempt_no, p_tool_name, 'RUNNING', now())
    returning * into attempt_row;
    return jsonb_build_object('id', attempt_row.id, 'step_id', step_row.id,
                              'attempt_no', attempt_row.attempt_no, 'status', attempt_row.status);
end;
$$;
revoke all on function public.start_research_attempt(uuid, uuid, text, integer, text) from public, anon, authenticated;
grant execute on function public.start_research_attempt(uuid, uuid, text, integer, text) to service_role;

create or replace function public.save_research_findings_if_active(
    p_task_id uuid, p_worker_id uuid, p_rows jsonb
) returns boolean
language plpgsql security definer set search_path = public, pg_temp as $$
declare task_row public.research_tasks%rowtype;
begin
    -- Shares the task row lock with cancellation. If this function gets the lock
    -- first, its write completes before the cancellation RPC can return. If
    -- cancellation gets it first, no subsequent finding can be inserted.
    select * into task_row from public.research_tasks where id = p_task_id for update;
    if not found or task_row.cancel_requested or task_row.worker_id is distinct from p_worker_id
       or task_row.status not in ('PLANNING','RUNNING','RECOVERING') then
        return false;
    end if;

    insert into public.findings(
        task_id, entity_key, entity_name, field_name, field_value, value_status,
        source_id, supporting_excerpt, verification_status, confidence_notes
    )
    select p_task_id, r.entity_key, r.entity_name, r.field_name, r.field_value,
           r.value_status, r.source_id, coalesce(r.supporting_excerpt, ''),
           r.verification_status, r.confidence_notes
      from jsonb_to_recordset(coalesce(p_rows, '[]'::jsonb)) as r(
        entity_key text, entity_name text, field_name text, field_value jsonb,
        value_status text, source_id uuid, supporting_excerpt text,
        verification_status text, confidence_notes text
      )
    on conflict (source_id, entity_key, field_name) do update set
        entity_name = excluded.entity_name,
        field_value = excluded.field_value,
        value_status = excluded.value_status,
        supporting_excerpt = excluded.supporting_excerpt,
        verification_status = excluded.verification_status,
        confidence_notes = excluded.confidence_notes,
        updated_at = now()
    where excluded.verification_status = 'rejected_semantic'
       or public.findings.value_status <> 'found'
       or excluded.value_status = 'found';
    return true;
end;
$$;
revoke all on function public.save_research_findings_if_active(uuid, uuid, jsonb) from public, anon, authenticated;
grant execute on function public.save_research_findings_if_active(uuid, uuid, jsonb) to service_role;

create or replace function public.request_research_cancellation(p_task_id uuid)
returns boolean language plpgsql security definer set search_path = public, pg_temp as $$
declare task_row public.research_tasks%rowtype;
begin
    select * into task_row from public.research_tasks where id = p_task_id for update;
    if not found then return false; end if;
    if task_row.status = 'CANCELLED' or task_row.cancel_requested then return true; end if;
    if task_row.status in ('COMPLETED','COMPLETED_WITH_LIMITATIONS','FAILED') then return false; end if;
    update public.research_tasks
       set cancel_requested = true,
           status = case when status = 'QUEUED' and worker_id is null then 'CANCELLED' else status end,
           completed_at = case when status = 'QUEUED' and worker_id is null then now() else completed_at end,
           updated_at = now()
     where id = p_task_id;
    perform public.append_research_task_event(
        p_task_id, 'task_cancel_requested',
        jsonb_build_object('task_id', p_task_id, 'status', 'CANCELLATION_REQUESTED')
    );
    if task_row.status = 'QUEUED' and task_row.worker_id is null then
        update public.research_steps set status = 'SKIPPED', completed_at = now(), updated_at = now()
         where task_id = p_task_id and status in ('PENDING','RUNNING','RETRYING');
        update public.research_step_attempts set status = 'INTERRUPTED', completed_at = now(),
               error_classification = 'cancelled', safe_error = 'Task was cancelled before a worker started.'
         where task_id = p_task_id and status = 'RUNNING';
        perform public.append_research_task_event(
            p_task_id, 'task_status', jsonb_build_object('task_id', p_task_id, 'status', 'CANCELLED')
        );
    end if;
    return true;
end;
$$;
revoke all on function public.request_research_cancellation(uuid) from public, anon, authenticated;
grant execute on function public.request_research_cancellation(uuid) to service_role;

create or replace function public.finish_research_cancellation(p_task_id uuid, p_worker_id uuid)
returns boolean language plpgsql security definer set search_path = public, pg_temp as $$
declare task_row public.research_tasks%rowtype;
begin
    select * into task_row from public.research_tasks where id = p_task_id for update;
    if not found then return false; end if;
    if task_row.status = 'CANCELLED' then
        -- Reconcile rows left pending by earlier cancellation handlers while
        -- keeping this RPC idempotent for already terminal tasks.
        update public.research_steps set status = 'SKIPPED', completed_at = now(), updated_at = now()
         where task_id = p_task_id and status in ('PENDING','RUNNING','RETRYING');
        update public.research_step_attempts set status = 'INTERRUPTED', completed_at = now(),
               error_classification = 'cancelled', safe_error = 'Cancellation was acknowledged before the attempt completed.'
         where task_id = p_task_id and status = 'RUNNING';
        return true;
    end if;
    if not task_row.cancel_requested
       or (task_row.worker_id is not null and task_row.worker_id is distinct from p_worker_id) then
        return false;
    end if;

    update public.research_steps set status = 'SKIPPED', completed_at = now(), updated_at = now()
     where task_id = p_task_id and status in ('PENDING','RUNNING','RETRYING');
    update public.research_step_attempts set status = 'INTERRUPTED', completed_at = now(),
           error_classification = 'cancelled', safe_error = 'Cancellation was acknowledged before the attempt completed.'
     where task_id = p_task_id and status = 'RUNNING';
    update public.research_tasks set status = 'CANCELLED', completed_at = now(),
           worker_id = null, lease_expires_at = null, updated_at = now()
     where id = p_task_id;
    perform public.append_research_task_event(
        p_task_id, 'task_status', jsonb_build_object('task_id', p_task_id, 'status', 'CANCELLED')
    );
    return true;
end;
$$;
revoke all on function public.finish_research_cancellation(uuid, uuid) from public, anon, authenticated;
grant execute on function public.finish_research_cancellation(uuid, uuid) to service_role;

create or replace function public.finalize_pending_research_cancellations()
returns integer language plpgsql security definer set search_path = public, pg_temp as $$
declare
    pending_task record;
    finalized integer := 0;
begin
    for pending_task in
        select id from public.research_tasks
         where cancel_requested and status <> 'CANCELLED'
           and status not in ('COMPLETED','COMPLETED_WITH_LIMITATIONS')
           and (worker_id is null or lease_expires_at is null or lease_expires_at < now())
         for update skip locked
    loop
        update public.research_steps set status = 'SKIPPED', completed_at = now(), updated_at = now()
         where task_id = pending_task.id and status in ('PENDING','RUNNING','RETRYING');
        update public.research_step_attempts set status = 'INTERRUPTED', completed_at = now(),
               error_classification = 'cancelled', safe_error = 'Cancellation was pending when the worker stopped.'
         where task_id = pending_task.id and status = 'RUNNING';
        update public.research_tasks set status = 'CANCELLED', completed_at = now(),
               worker_id = null, lease_expires_at = null, updated_at = now()
         where id = pending_task.id;
        perform public.append_research_task_event(
            pending_task.id, 'task_status', jsonb_build_object('task_id', pending_task.id, 'status', 'CANCELLED', 'recovered_after_restart', true)
        );
        finalized := finalized + 1;
    end loop;
    return finalized;
end;
$$;
revoke all on function public.finalize_pending_research_cancellations() from public, anon, authenticated;
grant execute on function public.finalize_pending_research_cancellations() to service_role;

create or replace function public.claim_research_task(p_task_id uuid, p_worker_id uuid, p_resume boolean)
returns boolean language plpgsql security definer set search_path = public, pg_temp as $$
declare claimed boolean := false;
begin
    if p_resume then
        update public.research_tasks t
           set status = 'RECOVERING', worker_id = p_worker_id,
               lease_expires_at = now() + interval '5 minutes', updated_at = now()
         where t.id = p_task_id and not t.cancel_requested
           and t.status in ('QUEUED','PLANNING','FAILED','COMPLETED_WITH_LIMITATIONS','RUNNING','RECOVERING')
           and (t.worker_id is null or t.lease_expires_at is null or t.lease_expires_at < now())
           and (
               exists (select 1 from public.research_steps s where s.task_id = t.id
                       and (s.status in ('PENDING','RUNNING','RETRYING') or
                            (s.status = 'FAILED' and coalesce(s.last_error_classification, 'unknown') not in ('authorization','validation'))))
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
        update public.research_tasks t set status = 'PLANNING', worker_id = p_worker_id,
               lease_expires_at = now() + interval '5 minutes', updated_at = now()
         where t.id = p_task_id and t.status = 'QUEUED' and not t.cancel_requested
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
        (old.status = 'COMPLETED_WITH_LIMITATIONS' and new.status = 'RECOVERING') or
        (old.status = 'FAILED' and new.status = 'CANCELLED' and old.cancel_requested)
    ) then raise exception 'Illegal research task status transition: % -> %', old.status, new.status; end if;
    if new.status in ('COMPLETED','COMPLETED_WITH_LIMITATIONS') and exists (
        select 1 from public.research_steps s where s.task_id = new.id and s.status in ('PENDING','RUNNING','RETRYING')
    ) then raise exception 'Cannot complete task while steps remain unresolved'; end if;
    return new;
end;
$$;

