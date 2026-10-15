-- Stage 4 artifact metadata. Apply manually after Stage 3 migrations.
create table if not exists public.artifacts (
    id uuid primary key,
    task_id uuid not null references public.research_tasks(id) on delete cascade,
    artifact_type text not null check (artifact_type in ('spreadsheet','email','report')),
    generation_key text not null,
    filename text not null,
    storage_backend text not null check (storage_backend in ('local','supabase')),
    storage_path text not null,
    mime_type text not null,
    status text not null check (status in ('READY','FAILED')),
    preview jsonb not null default '{}'::jsonb,
    content_hash text not null,
    error_summary text,
    created_at timestamptz not null default now(),
    unique (task_id, generation_key)
);
create index if not exists artifacts_task_created_idx on public.artifacts(task_id, created_at desc);
alter table public.artifacts enable row level security;
drop policy if exists artifacts_owner_read on public.artifacts;
create policy artifacts_owner_read on public.artifacts for select to authenticated
using (exists (select 1 from public.research_tasks t where t.id = task_id and t.user_id = (select auth.uid())));
grant select on public.artifacts to authenticated;
grant select, insert, update, delete on public.artifacts to service_role;
