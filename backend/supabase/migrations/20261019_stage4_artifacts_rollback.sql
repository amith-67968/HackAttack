-- Remove Stage 4 artifact metadata. Deletes artifact records; storage objects must
-- be removed separately from the private bucket or local ARTIFACT_DIR if desired.
drop table if exists public.artifacts;
