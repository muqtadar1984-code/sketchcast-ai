-- 0127_jobs_claimed_at — when a worker took the job (2026-10-14)
--
-- jobs carried created_at and updated_at only, so the wait in the queue could
-- not be separated from the work after the fact (the phase clock records it
-- per generation, in params.timings, but a column is queryable without the
-- blob and survives a deferral's restart). Set on every claim: the worker's
-- ordinary claim (worker/client.py) and the two transactional catalogue
-- claims of 0126, which are re-created here to write it. A requeue or a
-- deferral leaves it — it records the LAST claim.

alter table public.jobs add column if not exists claimed_at timestamptz;

create or replace function public.claim_catalogue_kit_job(p_exclude_types text[], p_max_kits integer)
returns setof public.jobs
language plpgsql security definer set search_path = public as $$
declare
  v_kits text[];
  v_only boolean;
  v_job  public.jobs%rowtype;
begin
  perform pg_advisory_xact_lock(hashtext('catalogue_claim'));
  select coalesce(array_agg(distinct j.params->>'kit_id'), '{}'::text[]) into v_kits
    from public.jobs j
   where j.status = 'processing' and j.params->>'catalogue' = 'true'
     and j.params->>'kit_id' is not null;
  v_only := coalesce(array_length(v_kits, 1), 0) >= greatest(1, p_max_kits);
  select j.* into v_job
    from public.jobs j
   where j.status = 'queued'
     and j.params->>'catalogue' = 'true'
     and (p_exclude_types is null or not (j.type = any (p_exclude_types)))
     and (j.params->>'deferred_until' is null
          or (j.params->>'deferred_until')::timestamptz < now())
     and (not v_only or j.params->>'kit_id' = any (v_kits))
   order by j.created_at
   limit 1
   for update skip locked;
  if not found then
    return;
  end if;
  update public.jobs set status = 'processing', progress = 1, claimed_at = now()
   where id = v_job.id and status = 'queued'
  returning * into v_job;
  if found then
    return next v_job;
  end if;
end $$;

create or replace function public.claim_catalogue_observer_job(p_types text[], p_max_jobs integer)
returns setof public.jobs
language plpgsql security definer set search_path = public as $$
declare
  v_n   integer;
  v_job public.jobs%rowtype;
begin
  perform pg_advisory_xact_lock(hashtext('catalogue_claim'));
  select count(*) into v_n from public.jobs j
   where j.status = 'processing' and j.type = any (p_types);
  if v_n >= greatest(1, p_max_jobs) then
    return;
  end if;
  select j.* into v_job
    from public.jobs j
   where j.status = 'queued'
     and j.type = any (p_types)
     and (j.params->>'deferred_until' is null
          or (j.params->>'deferred_until')::timestamptz < now())
   order by j.created_at
   limit 1
   for update skip locked;
  if not found then
    return;
  end if;
  update public.jobs set status = 'processing', progress = 1, claimed_at = now()
   where id = v_job.id and status = 'queued'
  returning * into v_job;
  if found then
    return next v_job;
  end if;
end $$;
