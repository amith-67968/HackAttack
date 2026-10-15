-- Roll back the 20261017 claim predicate hotfix by restoring the prior
-- Stage 3 function definition. This also removes finalization-only resume.
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
           and t.status in ('PLANNING','FAILED','COMPLETED_WITH_LIMITATIONS','RUNNING','RECOVERING')
           and (t.worker_id is null or t.lease_expires_at is null or t.lease_expires_at < now())
           and (
               exists (select 1 from public.research_steps s where s.task_id = t.id
                       and (s.status in ('PENDING','RUNNING','RETRYING') or
                            (s.status = 'FAILED' and coalesce(s.last_error_classification, 'unknown') not in
                             ('authorization','validation'))))
               or exists (select 1 from public.sources src where src.task_id = t.id
                          and src.fetch_status in ('discovered','retrieved','fetch_failed','extraction_failed'))
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
