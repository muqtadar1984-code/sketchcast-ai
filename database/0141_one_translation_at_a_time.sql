-- 0141_one_translation_at_a_time — Kit Translator T2, decision D7 (2026-10-17)
-- APPLIED to production 2026-10-17 ~18:20 UTC (Supabase apply_migration).
--
-- The worker-side gate (sketchcast-ai#263) read "is a translation in flight?"
-- in each poll thread and then claimed: two threads read "free" within the
-- same second and both claimed (jobs 90102c8c and c1ef5aa7, 17:57:56 UTC).
-- The rule belongs HERE, under the same advisory lock as the claim: while a
-- kit_translate job is processing, or a translated kit (language <> 'en')
-- is still generating, kit_translate is simply not among the claimable
-- types for this call. Everything else about the function is unchanged
-- (0126): the deployment-wide cap, oldest first, deferred_until honoured.
-- worker/run._catalogue_observer_types keeps its read as the log line and a
-- fast path; the database is the rule.
create or replace function public.claim_catalogue_observer_job(p_types text[], p_max_jobs integer)
returns setof public.jobs
language plpgsql security definer set search_path = public as $$
declare
  v_n     integer;
  v_job   public.jobs%rowtype;
  v_types text[] := p_types;
begin
  perform pg_advisory_xact_lock(hashtext('catalogue_claim'));
  if 'kit_translate' = any (v_types) then
    if exists (select 1 from public.jobs j where j.type = 'kit_translate' and j.status = 'processing')
       or exists (select 1 from public.topic_kits k
                   where k.status = 'generating' and coalesce(k.language, 'en') <> 'en') then
      v_types := array_remove(v_types, 'kit_translate');
    end if;
  end if;
  if coalesce(array_length(v_types, 1), 0) = 0 then
    return;
  end if;
  select count(*) into v_n from public.jobs j
   where j.status = 'processing' and j.type = any (p_types);
  if v_n >= greatest(1, p_max_jobs) then
    return;
  end if;
  select j.* into v_job
    from public.jobs j
   where j.status = 'queued'
     and j.type = any (v_types)
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

comment on function public.claim_catalogue_observer_job(text[], integer) is
  'The oldest queued catalogue observer job under the deployment-wide cap, claimed under one advisory lock (0126). 0141: kit_translate is claimable only while no translation is in flight — no kit_translate processing and no translated kit generating (Kit Translator T2, one kit at a time).';
