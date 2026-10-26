-- SK R1 retrieval test: schema for the Supabase BRANCH database only (sk-b1).
-- Every object is prefixed sk_r1_ and is dropped by teardown.sql at the end of the run.
-- Source tables (sk_book_blocks, topic_articles) are READ only; nothing here alters them.
--
-- Indexed: book blocks (and approved topic_article sections, when the branch carries them).
-- NEVER indexed: topic_questions, answers, marking schemes (plan §6). The insert below
-- selects from sk_book_blocks and topic_articles.sections only.

create extension if not exists vector;

-- Flatten any jsonb value to plain text (arrays/objects joined with spaces).
create or replace function sk_r1_flat(j jsonb) returns text
language plpgsql immutable as $$
begin
  return case jsonb_typeof(j)
    when 'array'  then (select coalesce(string_agg(sk_r1_flat(e), ' '), '') from jsonb_array_elements(j) e)
    when 'object' then (select coalesce(string_agg(sk_r1_flat(v), ' '), '') from jsonb_each(j) kv(k, v))
    when 'string' then j #>> '{}'
    when 'null'   then ''
    else coalesce(j::text, '')
  end;
end $$;

-- Text a block contributes to the index, by block_type.
-- think: prompt text only (its answer is treated as an answer key and excluded).
-- worked_example: problem + steps + answer (plan §6 names worked examples as a retrieval source).
create or replace function sk_r1_block_text(bt text, b jsonb) returns text
language sql immutable as $$
  select regexp_replace(regexp_replace(case bt
    when 'paragraph'      then b->>'text'
    when 'key_idea'       then b->>'text'
    when 'extension'      then b->>'text'
    when 'think'          then b->>'text'
    when 'key_term'       then coalesce(b->>'term', '') || ': ' || coalesce(b->>'text', '')
    when 'misconception'  then 'Common mistake: ' || coalesce(b->>'wrong', '') || ' Correct idea: ' || coalesce(b->>'right', '')
    when 'figure'         then coalesce(b->>'caption', '')
    when 'list'           then sk_r1_flat(b->'items')
    when 'table'          then coalesce(b->>'caption', '') || ' ' || sk_r1_flat(b->'columns') || ' ' || sk_r1_flat(b->'rows')
    when 'worked_example' then coalesce(b->>'problem', '') || ' ' || sk_r1_flat(b->'steps') || ' ' || coalesce(b->>'answer', '')
    else sk_r1_flat(b) end, '\*+', '', 'g'), '\s+', ' ', 'g')
$$;

create table if not exists sk_r1_docs (
  id          uuid primary key default gen_random_uuid(),
  src_kind    text not null,            -- block | article_section | fake_product | fake_chapter
  src_id      uuid not null,            -- sk_book_blocks.id or topic_articles.id (copies keep the original id)
  src_index   int,
  product_id  uuid not null,
  chapter     int  not null,
  unit_key    text,
  block_type  text,
  content     text not null,
  tsv         tsvector generated always as (to_tsvector('english', content)) stored,
  embedding   vector(1024)
);
create index if not exists sk_r1_docs_tsv_gin    on sk_r1_docs using gin (tsv);
create index if not exists sk_r1_docs_filter_idx on sk_r1_docs (product_id, chapter);

-- Real blocks of the real product.
insert into sk_r1_docs (src_kind, src_id, src_index, product_id, chapter, unit_key, block_type, content)
select 'block', b.id, b.block_index, b.product_id, b.chapter, b.unit_key, b.block_type, sk_r1_block_text(b.block_type, b.body)
from sk_book_blocks b
where length(sk_r1_block_text(b.block_type, b.body)) > 0;

-- Approved article sections (0 rows on sk-b1: the branch copy of the article has an empty body by design).
insert into sk_r1_docs (src_kind, src_id, src_index, product_id, chapter, unit_key, block_type, content)
select 'article_section', a.id, s.ord::int - 1, p.id, 2, null, 'article_section',
       regexp_replace(coalesce(s.val->>'heading', s.val->>'title', '') || ' ' || sk_r1_flat(coalesce(s.val->'body', s.val->'paragraphs', s.val->'content', s.val->'text')), '\s+', ' ', 'g')
from topic_articles a
join sk_book_products p on p.slug = 'cells-structure-function-organisation'
cross join lateral jsonb_array_elements(a.sections) with ordinality s(val, ord)
where a.status = 'approved' and jsonb_typeof(a.sections) = 'array';

-- FAKE product (same chapter number) and FAKE chapter (same product): identical text, so any
-- filter failure would surface as a tie in the results. Zero rows from these may ever be returned.
insert into sk_r1_docs (src_kind, src_id, src_index, product_id, chapter, unit_key, block_type, content)
select 'fake_product', src_id, src_index, '00000000-0000-4000-8000-00000000fa4e'::uuid, chapter, unit_key, block_type, content
from sk_r1_docs where src_kind = 'block';
insert into sk_r1_docs (src_kind, src_id, src_index, product_id, chapter, unit_key, block_type, content)
select 'fake_chapter', src_id, src_index, product_id, 9, unit_key, block_type, content
from sk_r1_docs where src_kind = 'block';

-- HNSW index (cosine). Built before embeddings exist; pgvector maintains it on update.
create index if not exists sk_r1_docs_emb_hnsw on sk_r1_docs using hnsw (embedding vector_cosine_ops);

-- Lock the table away from the API roles; everything goes through the SECURITY DEFINER functions below.
alter table sk_r1_docs enable row level security;
revoke all on sk_r1_docs from anon, authenticated;

-- Dump (no embeddings) so the bench can judge gold blocks and embed the content.
create or replace function sk_r1_dump() returns jsonb
language sql stable security definer set search_path = public as $$
  select coalesce(jsonb_agg(jsonb_build_object('id', id, 'src_kind', src_kind, 'src_id', src_id, 'src_index', src_index,
           'product_id', product_id, 'chapter', chapter, 'unit_key', unit_key, 'block_type', block_type, 'content', content,
           'has_embedding', embedding is not null) order by src_kind, product_id, chapter, unit_key, src_index), '[]'::jsonb)
  from sk_r1_docs
$$;

-- Write embeddings for real docs, then mirror them onto the fake copies (same text, same vector).
create or replace function sk_r1_set_embeddings(p jsonb) returns int
language plpgsql security definer set search_path = public as $$
declare n int;
begin
  update sk_r1_docs d set embedding = (e.value->>'embedding')::vector(1024)
  from jsonb_array_elements(p) e
  where d.id = (e.value->>'id')::uuid and d.src_kind in ('block', 'article_section');
  get diagnostics n = row_count;
  update sk_r1_docs f set embedding = r.embedding
  from sk_r1_docs r
  where r.src_kind in ('block', 'article_section') and r.embedding is not null
    and f.src_kind in ('fake_product', 'fake_chapter') and f.src_id = r.src_id
    and coalesce(f.src_index, -1) = coalesce(r.src_index, -1) and f.embedding is null;
  return n;
end $$;

-- mode 'and' = websearch_to_tsquery as written (every term required);
-- mode 'or'  = the same stemmed lexemes joined with |, ranked by ts_rank_cd (coverage).
create or replace function sk_r1_tsquery(q text, mode text) returns tsquery
language sql immutable as $$
  select case when mode = 'or'
    then to_tsquery('english', coalesce(nullif(array_to_string(tsvector_to_array(to_tsvector('english', q)), ' | '), ''), 'zzzznomatch'))
    else websearch_to_tsquery('english', q) end
$$;

-- Method 1: Postgres FTS + ts_rank_cd. db_ms is clock_timestamp() inside the function.
create or replace function sk_r1_search_fts(q text, p_product uuid, p_chapter int, k int default 5, mode text default 'and') returns jsonb
language plpgsql stable security definer set search_path = public as $$
declare t0 timestamptz := clock_timestamp(); tsq tsquery; rows jsonb;
begin
  tsq := sk_r1_tsquery(q, mode);
  select coalesce(jsonb_agg(jsonb_build_object('id', id, 'src_kind', src_kind, 'src_id', src_id, 'unit_key', unit_key,
           'product_id', product_id, 'chapter', chapter, 'score', score) order by score desc, id), '[]'::jsonb)
  into rows
  from (select d.id, d.src_kind, d.src_id, d.unit_key, d.product_id, d.chapter, ts_rank_cd(d.tsv, tsq) as score
        from sk_r1_docs d
        where d.product_id = p_product and d.chapter = p_chapter and d.tsv @@ tsq
        order by score desc, d.id limit k) s;
  return jsonb_build_object('rows', rows, 'db_ms', extract(epoch from clock_timestamp() - t0) * 1000, 'tsquery', tsq::text);
end $$;

-- Method 2: pgvector cosine (score = 1 - cosine distance).
create or replace function sk_r1_search_vec(qv vector(1024), p_product uuid, p_chapter int, k int default 5) returns jsonb
language plpgsql stable security definer set search_path = public as $$
declare t0 timestamptz := clock_timestamp(); rows jsonb;
begin
  select coalesce(jsonb_agg(jsonb_build_object('id', id, 'src_kind', src_kind, 'src_id', src_id, 'unit_key', unit_key,
           'product_id', product_id, 'chapter', chapter, 'score', score) order by score desc, id), '[]'::jsonb)
  into rows
  from (select d.id, d.src_kind, d.src_id, d.unit_key, d.product_id, d.chapter, 1 - (d.embedding <=> qv) as score
        from sk_r1_docs d
        where d.product_id = p_product and d.chapter = p_chapter and d.embedding is not null
        order by d.embedding <=> qv, d.id limit k) s;
  return jsonb_build_object('rows', rows, 'db_ms', extract(epoch from clock_timestamp() - t0) * 1000);
end $$;

-- Method 3: hybrid, reciprocal rank fusion of the FTS and vector top-`pool` lists (rrf_k = 60).
create or replace function sk_r1_search_hybrid(q text, qv vector(1024), p_product uuid, p_chapter int,
                                               k int default 5, pool int default 20, rrf_k int default 60, mode text default 'or') returns jsonb
language plpgsql stable security definer set search_path = public as $$
declare t0 timestamptz := clock_timestamp(); tsq tsquery; rows jsonb;
begin
  tsq := sk_r1_tsquery(q, mode);
  with f as (
    select d.id, ts_rank_cd(d.tsv, tsq) as s, row_number() over (order by ts_rank_cd(d.tsv, tsq) desc, d.id) as r
    from sk_r1_docs d
    where d.product_id = p_product and d.chapter = p_chapter and d.tsv @@ tsq
    order by s desc, d.id limit pool),
  v as (
    select d.id, 1 - (d.embedding <=> qv) as s, row_number() over (order by d.embedding <=> qv, d.id) as r
    from sk_r1_docs d
    where d.product_id = p_product and d.chapter = p_chapter and d.embedding is not null
    order by d.embedding <=> qv, d.id limit pool),
  u as (select id from f union select id from v)
  select coalesce(jsonb_agg(jsonb_build_object('id', id, 'src_kind', src_kind, 'src_id', src_id, 'unit_key', unit_key,
           'product_id', product_id, 'chapter', chapter, 'score', score, 'fts_score', fts_score, 'vec_score', vec_score)
           order by score desc, id), '[]'::jsonb)
  into rows
  from (select d.id, d.src_kind, d.src_id, d.unit_key, d.product_id, d.chapter,
               coalesce(1.0 / (rrf_k + f.r), 0) + coalesce(1.0 / (rrf_k + v.r), 0) as score,
               f.s as fts_score, v.s as vec_score
        from u join sk_r1_docs d on d.id = u.id
        left join f on f.id = u.id left join v on v.id = u.id
        order by score desc, d.id limit k) s;
  return jsonb_build_object('rows', rows, 'db_ms', extract(epoch from clock_timestamp() - t0) * 1000, 'tsquery', tsq::text);
end $$;

grant execute on function sk_r1_dump(), sk_r1_set_embeddings(jsonb), sk_r1_tsquery(text, text),
  sk_r1_search_fts(text, uuid, int, int, text), sk_r1_search_vec(vector, uuid, int, int),
  sk_r1_search_hybrid(text, vector, uuid, int, int, int, int, text) to anon, authenticated;
