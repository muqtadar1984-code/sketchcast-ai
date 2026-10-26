-- Drops every sk_r1_ object created by schema.sql. Run on the BRANCH database at the end of the test.
-- The vector extension is left installed (harmless, shared); nothing outside the sk_r1_ prefix is touched.
drop function if exists sk_r1_search_hybrid(text, vector, uuid, int, int, int, int, text);
drop function if exists sk_r1_search_vec(vector, uuid, int, int);
drop function if exists sk_r1_search_fts(text, uuid, int, int, text);
drop function if exists sk_r1_tsquery(text, text);
drop function if exists sk_r1_set_embeddings(jsonb);
drop function if exists sk_r1_dump();
drop table if exists sk_r1_docs;
drop function if exists sk_r1_block_text(text, jsonb);
drop function if exists sk_r1_flat(jsonb);
-- Verify: expect 0 rows.
select n.nspname, c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace where c.relname like 'sk_r1_%'
union all
select n.nspname, p.proname from pg_proc p join pg_namespace n on n.oid = p.pronamespace where p.proname like 'sk_r1_%';
