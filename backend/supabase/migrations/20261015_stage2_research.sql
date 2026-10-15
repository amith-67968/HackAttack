-- ProofLoop Stage 2 research persistence. Apply in the Supabase SQL editor or via CLI.
create extension if not exists pgcrypto;

create table if not exists public.research_tasks (
    id uuid primary key default gen_random_uuid(),
    user_id uuid references auth.users(id) on delete cascade,
    query text not null,
    normalized_topic text,
    output_type text not null default 'auto' check (output_type in ('auto','spreadsheet','email','report')),
    research_depth text not null default 'standard' check (research_depth in ('standard','deep')),
    status text not null check (status in ('QUEUED','PLANNING','RUNNING','VERIFYING','RECOVERING','GENERATING_OUTPUT','AWAITING_APPROVAL','COMPLETED','COMPLETED_WITH_LIMITATIONS','FAILED','CANCELLED')),
    config jsonb not null default '{}'::jsonb,
    plan jsonb,
    summary text,
    error_summary text,
    started_at timestamptz,
    completed_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.research_steps (
    id uuid primary key default gen_random_uuid(),
    task_id uuid not null references public.research_tasks(id) on delete cascade,
    action_key text not null,
    sequence_no integer not null,
    action_type text not null,
    title text not null,
    input jsonb not null default '{}'::jsonb,
    expected_result jsonb not null default '{}'::jsonb,
    verification_rule jsonb not null default '{}'::jsonb,
    status text not null default 'PENDING' check (status in ('PENDING','RUNNING','SUCCEEDED','VERIFIED','RETRYING','FAILED','SKIPPED','AWAITING_APPROVAL')),
    attempt_count integer not null default 0,
    result jsonb,
    last_error text,
    started_at timestamptz,
    completed_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (task_id, action_key)
);

create table if not exists public.sources (
    id uuid primary key default gen_random_uuid(),
    task_id uuid not null references public.research_tasks(id) on delete cascade,
    url text not null,
    canonical_url text not null,
    title text,
    domain text not null,
    source_type text not null default 'web',
    retrieved_at timestamptz,
    published_at timestamptz,
    content_excerpt text,
    content_hash text,
    quality_notes text,
    fetch_status text not null default 'discovered',
    created_at timestamptz not null default now(),
    unique (task_id, canonical_url)
);

create table if not exists public.findings (
    id uuid primary key default gen_random_uuid(),
    task_id uuid not null references public.research_tasks(id) on delete cascade,
    entity_key text not null,
    entity_name text not null,
    field_name text not null,
    field_value jsonb,
    value_status text not null check (value_status in ('found','not_found','not_disclosed','unverified','conflicting')),
    source_id uuid not null references public.sources(id) on delete cascade,
    supporting_excerpt text not null default '',
    verification_status text not null,
    confidence_notes text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (source_id, entity_key, field_name)
);

grant select, insert, update, delete on public.research_tasks, public.research_steps, public.sources, public.findings to authenticated, service_role;

create index if not exists research_tasks_created_idx on public.research_tasks(created_at desc);
create index if not exists research_tasks_status_idx on public.research_tasks(status, updated_at desc);
create index if not exists research_steps_task_sequence_idx on public.research_steps(task_id, sequence_no);
create index if not exists sources_task_idx on public.sources(task_id, created_at);
create index if not exists findings_task_idx on public.findings(task_id, entity_name, field_name);
create index if not exists findings_source_idx on public.findings(source_id);

alter table public.research_tasks enable row level security;
alter table public.research_steps enable row level security;
alter table public.sources enable row level security;
alter table public.findings enable row level security;

-- Authenticated users can only see and mutate their own rows. The backend uses
-- the service-role key for the documented loopback-only, single-user Stage 2 mode.
drop policy if exists research_tasks_owner on public.research_tasks;
create policy research_tasks_owner on public.research_tasks
    for all to authenticated using (user_id = (select auth.uid()))
    with check (user_id = (select auth.uid()));

drop policy if exists research_steps_owner on public.research_steps;
create policy research_steps_owner on public.research_steps
    for all to authenticated using (
        exists (select 1 from public.research_tasks t where t.id = task_id and t.user_id = (select auth.uid())
    )) with check (
        exists (select 1 from public.research_tasks t where t.id = task_id and t.user_id = (select auth.uid())
    ));

drop policy if exists sources_owner on public.sources;
create policy sources_owner on public.sources
    for all to authenticated using (
        exists (select 1 from public.research_tasks t where t.id = task_id and t.user_id = (select auth.uid())
    )) with check (
        exists (select 1 from public.research_tasks t where t.id = task_id and t.user_id = (select auth.uid())
    ));

drop policy if exists findings_owner on public.findings;
create policy findings_owner on public.findings
    for all to authenticated using (
        exists (select 1 from public.research_tasks t where t.id = task_id and t.user_id = (select auth.uid())
    )) with check (
        exists (select 1 from public.research_tasks t where t.id = task_id and t.user_id = (select auth.uid())
    ));
