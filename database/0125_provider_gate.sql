-- 0125_provider_gate — the shared half of shared/provider_gate.py (2026-10-14)
--
-- Both Gemini models the worker calls on Vertex sit on Google's shared
-- pay-as-you-go pool: a 429 means the pool is busy, and sending more buys
-- refusals, not pictures. Each worker process keeps a local gate; these two
-- tables let several replicas share ONE in-flight cap (leases with a TTL,
-- so a crashed holder frees its slot) and ONE learned pacing state
-- (rate_per_min climbs on clean calls, halves on a refusal, and a refusal
-- silences every replica for the cooldown).
--
-- Service-role only: the worker is the sole caller. RLS is on with no
-- policies, so app users cannot read or write these rows.

create table if not exists public.provider_leases (
  id          uuid primary key default gen_random_uuid(),
  provider    text not null,
  holder      text not null,
  acquired_at timestamptz not null default now(),
  expires_at  timestamptz not null
);
create index if not exists provider_leases_provider_expires_idx
  on public.provider_leases (provider, expires_at);
alter table public.provider_leases enable row level security;

create table if not exists public.provider_pacing (
  provider       text primary key,
  rate_per_min   numeric not null,
  cooldown_until timestamptz,
  successes      integer not null default 0,
  refusals       integer not null default 0,
  updated_at     timestamptz not null default now()
);
alter table public.provider_pacing enable row level security;

-- One slot on `p_provider`, or NULL when `p_max` holders are already in
-- flight. Expired leases are swept first; the advisory lock makes the
-- count-then-insert atomic across callers.
create or replace function public.acquire_provider_lease(
  p_provider text, p_max integer, p_holder text, p_ttl_seconds integer)
returns uuid
language plpgsql security definer set search_path = public as $$
declare
  v_id uuid;
  v_n  integer;
begin
  perform pg_advisory_xact_lock(hashtext('provider_lease:' || p_provider));
  delete from public.provider_leases
   where provider = p_provider and expires_at < now();
  select count(*) into v_n from public.provider_leases where provider = p_provider;
  if v_n >= greatest(1, p_max) then
    return null;
  end if;
  insert into public.provider_leases (provider, holder, expires_at)
  values (p_provider, p_holder, now() + make_interval(secs => greatest(1, p_ttl_seconds)))
  returning id into v_id;
  return v_id;
end $$;

create or replace function public.release_provider_lease(p_id uuid)
returns void
language sql security definer set search_path = public as $$
  delete from public.provider_leases where id = p_id;
$$;

-- Record one call's outcome and return the shared pacing state. AIMD:
-- `p_step_after` clean calls in a row raise the rate by one (to the
-- ceiling); a refusal halves it (to the floor), zeroes the streak and
-- silences the provider for `p_cooldown_seconds` or the server's
-- Retry-After, whichever is longer. A rate of 0 means "unpaced" and is
-- left at 0.
create or replace function public.provider_outcome(
  p_provider text, p_ok boolean, p_start numeric, p_floor numeric, p_ceiling numeric,
  p_step_after integer, p_cooldown_seconds numeric, p_retry_after numeric default null)
returns table (rate_per_min numeric, cooldown_until timestamptz, successes integer, refusals integer)
language plpgsql security definer set search_path = public as $$
declare
  r public.provider_pacing%rowtype;
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
    r.cooldown_until := greatest(coalesce(r.cooldown_until, now()),
                                 now() + make_interval(secs => greatest(p_cooldown_seconds, coalesce(p_retry_after, 0))));
  end if;
  update public.provider_pacing p
     set rate_per_min = r.rate_per_min, cooldown_until = r.cooldown_until,
         successes = r.successes, refusals = r.refusals, updated_at = now()
   where p.provider = p_provider;
  return query select r.rate_per_min, r.cooldown_until, r.successes, r.refusals;
end $$;

revoke all on table public.provider_leases from anon, authenticated;
revoke all on table public.provider_pacing from anon, authenticated;
revoke execute on function public.acquire_provider_lease(text, integer, text, integer) from public, anon, authenticated;
revoke execute on function public.release_provider_lease(uuid) from public, anon, authenticated;
revoke execute on function public.provider_outcome(text, boolean, numeric, numeric, numeric, integer, numeric, numeric) from public, anon, authenticated;
grant execute on function public.acquire_provider_lease(text, integer, text, integer) to service_role;
grant execute on function public.release_provider_lease(uuid) to service_role;
grant execute on function public.provider_outcome(text, boolean, numeric, numeric, numeric, integer, numeric, numeric) to service_role;
