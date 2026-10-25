-- Book-side asset knowledge, LOCAL ONLY (founder 2026-10-25, decision 4: local
-- first; a production migration only after the book policy is proven).
--
-- asset_identity: what an external picture really IS, read from its source
-- (Commons API / the provider page), with the licence EVIDENCE for the copy we
-- hold. A licence recorded when the copy was obtained stays valid even if the
-- source later shows a newer one (CC licences are irrevocable); both are kept.
--
-- book_assets: book representations of library concepts (colour illustrations
-- generated for print). Kept out of the shared production library so the video
-- engine never selects them; registered there only by a later decision.

create table if not exists pub.asset_identity (
  asset_ref        text primary key,          -- asset:<uuid>
  source_url       text,
  title            text,
  description      text,
  author           text,
  licence_recorded text,                      -- what our library row holds
  licence_seen     text,                      -- what the source shows today
  licence_evidence jsonb not null default '{}',
  suitability      text not null default 'ok', -- ok | medical | unverified
  checked_at       timestamptz not null default now()
);

create table if not exists pub.book_assets (
  key            text primary key,            -- book:<key>
  path           text not null,               -- local file
  representation text not null,               -- colour_illustration | photo | diagram_vector
  concept        text not null,
  prompt         text,
  model          text,
  description    text,
  approved_by    text,
  created_at     timestamptz not null default now()
);
