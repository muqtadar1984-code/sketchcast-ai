-- 0142_translate_lane_orphan — Kit Translator T2 (2026-10-18)
-- APPLIED to production 2026-10-18 (Supabase apply_migration).
--
-- 0141 refused every kit_translate while a translated kit was `generating`.
-- A translate job cut off after it created its kit row (the label-fix
-- redeploy at 01:11 UTC; the reaper requeued it at 01:17) left that row
-- generating — and the rule then refused the very job that resumes it
-- (find_translation_kit picks up a generating, staged kit). Deadlock.
--
-- Now a generating translated kit whose OWN translate job is queued (same
-- source_kit_id and language) is an orphan, not work in flight. While one
-- exists, ONLY the orphans' jobs are claimable (oldest first), so the lane
-- still runs one translation at a time. Any other generating translated kit
-- still blocks every kit_translate, as before.
create or replace function public.claim_catalogue_observer_job(p_types text[], p_max_jobs integer)
returns setof public.jobs
language plpgsql security definer set search_path = public as $$
declare
  v_n       integer;
  v_job     public.jobs%rowtype;
  v_types   text[] := p_types;
  v_only    uuid[] := null;      -- when set: the only kit_translate jobs claimable
begin
  perform pg_advisory_xact_lock(hashtext('catalogue_claim'));
  if 'kit_translate' = any (v_types) then
    if exists (select 1 from public.jobs j where j.type = 'kit_translate' and j.status = 'processing')
       or exists (select 1 from public.topic_kits k
                   where k.status = 'generating' and coalesce(k.language, 'en') <> 'en'
                     and not exists (select 1 from public.jobs q
                                      where q.type = 'kit_translate' and q.status = 'queued'
                                        and q.params->>'source_kit_id' = k.source_kit_id::text
                                        and q.params->>'language' = k.language)) then
      v_types := array_remove(v_types, 'kit_translate');
    else
      select array_agg(q.id) into v_only
        from public.jobs q
        join public.topic_kits k
          on k.status = 'generating' and coalesce(k.language, 'en') <> 'en'
         and q.params->>'source_kit_id' = k.source_kit_id::text
         and q.params->>'language' = k.language
       where q.type = 'kit_translate' and q.status = 'queued';
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
     and (v_only is null or j.type <> 'kit_translate' or j.id = any (v_only))
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
  'The oldest queued catalogue observer job under the deployment-wide cap, one advisory lock (0126). kit_translate: claimable only while no translation is in flight (0141); a generating translated kit whose own translate job is queued is an orphan, and only that job may be claimed (0142).';
