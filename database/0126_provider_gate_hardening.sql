-- 0126_provider_gate_hardening — the gate becomes deployment-wide (2026-10-14)
--
-- 0125 shared the in-flight cap (leases) and the LEARNED pace across
-- replicas, but each process kept its own one-minute send window, the
-- "one kit at a time" rule was a read followed by a claim, a lease lasted
-- exactly as long as a text call may, and the SQL outcome function applied
-- no cap to a server-supplied Retry-After. This migration closes all four:
--
--   provider_permit            one atomic "may I send now?" per provider:
--                              the cooldown AND the send window live here,
--                              under the provider's advisory lock, so N
--                              replicas together send the learned rate.
--   provider_outcome           takes the cooldown cap the Python side applies
--                              (the 8-argument form stays as a wrapper so a
--                              worker still running 0125 code keeps working).
--   renew_provider_lease       a holder extends its lease while a long call
--                              runs; leases can now be short (180 s).
--   claim_catalogue_kit_job    the kit admission AND the claim in one
--   claim_catalogue_observer_job   transaction under one lock: two workers
--                              cannot both start a kit, or both take the
--                              cap's last figure slot.
--
-- Service-role only, as 0125.

-- ── the send window ────────────────────────────────────────────────────────
create table if not exists public.provider_sends (
  provider text not null,
  sent_at  timestamptz not null default now()
);
create index if not exists provider_sends_provider_sent_idx
  on public.provider_sends (provider, sent_at);
alter table public.provider_sends enable row level security;

-- May one more request go out on `p_provider` right now?
--   granted=true  → it may; the send is recorded.
--   granted=false → wait `wait_seconds` and ask again; `reason` says why
--                   ('cooldown' — a refusal silenced the provider;
--                    'pace' — the learned per-minute rate is spent).
-- The pacing row is created at `p_start` when the provider is new.
create or replace function public.provider_permit(
  p_provider text, p_start numeric, p_floor numeric, p_ceiling numeric)
returns table (granted boolean, wait_seconds numeric, reason text, rate_per_min numeric)
language plpgsql security definer set search_path = public as $$
declare
  r        public.provider_pacing%rowtype;
  v_n      integer;
  v_oldest timestamptz;
  v_rate   integer;
begin
  perform pg_advisory_xact_lock(hashtext('provider_pacing:' || p_provider));
  insert into public.provider_pacing (provider, rate_per_min)
  values (p_provider, p_start)
  on conflict (provider) do nothing;
  select * into r from public.provider_pacing where provider = p_provider;
  if r.cooldown_until is not null and r.cooldown_until > now() then
    return query select false, round(extract(epoch from (r.cooldown_until - now()))::numeric, 2),
                        'cooldown'::text, r.rate_per_min;
    return;
  end if;
  delete from public.provider_sends
   where provider = p_provider and sent_at < now() - interval '60 seconds';
  if r.rate_per_min > 0 then
    v_rate := greatest(1, floor(r.rate_per_min)::integer);
    select count(*), min(sent_at) into v_n, v_oldest
      from public.provider_sends where provider = p_provider;
    if v_n >= v_rate then
      return query select false,
                          round((60 - extract(epoch from (now() - v_oldest)) + 0.01)::numeric, 2),
                          'pace'::text, r.rate_per_min;
      return;
    end if;
  end if;
  insert into public.provider_sends (provider) values (p_provider);
  return query select true, 0::numeric, 'ok'::text, r.rate_per_min;
end $$;

-- ── the outcome, with the cooldown cap the Python side applies ─────────────
create or replace function public.provider_outcome(
  p_provider text, p_ok boolean, p_start numeric, p_floor numeric, p_ceiling numeric,
  p_step_after integer, p_cooldown_seconds numeric, p_retry_after numeric, p_max_cooldown numeric)
returns table (rate_per_min numeric, cooldown_until timestamptz, successes integer, refusals integer)
language plpgsql security definer set search_path = public as $$
declare
  r        public.provider_pacing%rowtype;
  v_silence numeric;
begin
  perform pg_advisory_xact_lock(hashtext('provider_pacing:' || p_provider));
  insert into public.provider_pacing (provider, rate_per_min)
  values (p_provider, p_start)
  on conflict (provider) do nothing;
  select * into r from public.provider_pacing where provider = p_provider for update;
  if p_ok then
    r.successes := r.successes + 1;
    if r.rate_per_min > 0 and r.successes >= greatest(1, p_step_after) then
      r.rate_per_min := least(p_ceiling, r.rate_per_min + 1);
      r.successes := 0;
    end if;
  else
    r.refusals := r.refusals + 1;
    r.successes := 0;
    if r.rate_per_min > 0 then
      r.rate_per_min := greatest(p_floor, r.rate_per_min / 2);
    end if;
    -- the same rule as ProviderGate.outcome: the longer of our cooldown and
    -- the server's Retry-After, never above the cap
    v_silence := least(coalesce(p_max_cooldown, 120),
                       greatest(p_cooldown_seconds, coalesce(p_retry_after, 0)));
    r.cooldown_until := greatest(coalesce(r.cooldown_until, now()),
                                 now() + make_interval(secs => v_silence));
  end if;
  update public.provider_pacing p
     set rate_per_min = r.rate_per_min, cooldown_until = r.cooldown_until,
         successes = r.successes, refusals = r.refusals, updated_at = now()
   where p.provider = p_provider;
  return query select r.rate_per_min, r.cooldown_until, r.successes, r.refusals;
end $$;

-- the 0125 signature: a worker still on 0125 code keeps working (cap 120 s)
create or replace function public.provider_outcome(
  p_provider text, p_ok boolean, p_start numeric, p_floor numeric, p_ceiling numeric,
  p_step_after integer, p_cooldown_seconds numeric, p_retry_after numeric default null)
returns table (rate_per_min numeric, cooldown_until timestamptz, successes integer, refusals integer)
language sql security definer set search_path = public as $$
  select * from public.provider_outcome(p_provider, p_ok, p_start, p_floor, p_ceiling,
                                        p_step_after, p_cooldown_seconds, p_retry_after, 120::numeric);
$$;

-- ── lease renewal ──────────────────────────────────────────────────────────
create or replace function public.renew_provider_lease(p_id uuid, p_ttl_seconds integer)
returns boolean
language plpgsql security definer set search_path = public as $$
declare v_n integer;
begin
  update public.provider_leases
     set expires_at = now() + make_interval(secs => greatest(1, p_ttl_seconds))
   where id = p_id and expires_at > now();
  get diagnostics v_n = row_count;
  return v_n > 0;
end $$;

-- ── transactional catalogue claims ─────────────────────────────────────────
-- A catalogue KIT job: the one-kit-at-a-time rule and the claim in one
-- transaction. While `p_max_kits` kits have a processing job, only THEIR
-- remaining jobs are candidates; otherwise the oldest queued kit job.
-- Returns the claimed row (now processing), or nothing.
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
  update public.jobs set status = 'processing', progress = 1
   where id = v_job.id and status = 'queued'
  returning * into v_job;
  if found then
    return next v_job;
  end if;
end $$;

-- A catalogue OBSERVER job (figure renders, harvests, …) under the
-- deployment-wide cap `p_max_jobs`, claimed in the same transaction as the
-- count.
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
  update public.jobs set status = 'processing', progress = 1
   where id = v_job.id and status = 'queued'
  returning * into v_job;
  if found then
    return next v_job;
  end if;
end $$;

-- ── grants ─────────────────────────────────────────────────────────────────
revoke all on table public.provider_sends from anon, authenticated;
revoke execute on function public.provider_permit(text, numeric, numeric, numeric) from public, anon, authenticated;
revoke execute on function public.provider_outcome(text, boolean, numeric, numeric, numeric, integer, numeric, numeric, numeric) from public, anon, authenticated;
revoke execute on function public.renew_provider_lease(uuid, integer) from public, anon, authenticated;
revoke execute on function public.claim_catalogue_kit_job(text[], integer) from public, anon, authenticated;
revoke execute on function public.claim_catalogue_observer_job(text[], integer) from public, anon, authenticated;
grant execute on function public.provider_permit(text, numeric, numeric, numeric) to service_role;
grant execute on function public.provider_outcome(text, boolean, numeric, numeric, numeric, integer, numeric, numeric, numeric) to service_role;
grant execute on function public.renew_provider_lease(uuid, integer) to service_role;
grant execute on function public.claim_catalogue_kit_job(text[], integer) to service_role;
grant execute on function public.claim_catalogue_observer_job(text[], integer) to service_role;
