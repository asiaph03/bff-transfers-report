-- Preserve source (ingestion) order in public.ringcx_inbound as a durable column, source_seq.
--
-- WHY: the dashboard's approved rule U2 gives same-day agent ties to the first row in the sheet's order.
-- The live table has no column that records that order. Read-only checks (Oct 24, 2026) showed the table's
-- physical insertion order (ctid) reproduces it: first- and last-agent tallies for all 59 same-day
-- multi-agent cases match the sheet for all 18 agents, and pg_stat_user_tables shows 222,165 rows inserted,
-- 0 updated, 0 deleted (a single straight load). ctid is NOT durable: UPDATE, DELETE + reuse, VACUUM FULL,
-- CLUSTER, pg_repack and dump/restore can change it. So capture it once, now, into source_seq.
--
-- WHO RUNS THIS: the table owner only, in a maintenance window, when the loader is paused.
-- The dashboard project never runs it. Part 1 and Part 3 are read-only; Part 2 changes the table.

-- ===== Part 1: read-only pre-checks (run first; all must hold) =====
SET default_transaction_read_only = on;
BEGIN READ ONLY;
SELECT n_tup_ins, n_tup_upd, n_tup_del, n_dead_tup, n_live_tup, vacuum_count
FROM pg_stat_user_tables WHERE schemaname = 'public' AND relname = 'ringcx_inbound';
-- expect: n_tup_upd = 0, n_tup_del = 0, n_dead_tup = 0, n_live_tup = row count, vacuum_count = 0
-- (vacuum_count counts manual VACUUM; a VACUUM FULL since the load would void the ctid order)
ROLLBACK;

-- ===== Part 2: the owner's change (one transaction; review before COMMIT) =====
SET default_transaction_read_only = off;     -- this session only
BEGIN;
-- Block concurrent loads/updates while the order is copied.
LOCK TABLE public.ringcx_inbound IN SHARE ROW EXCLUSIVE MODE;

ALTER TABLE public.ringcx_inbound ADD COLUMN source_seq bigint;

-- Number every existing row in physical (insertion) order. The subquery reads the snapshot taken before
-- the UPDATE, so the numbering follows the original ctid order even though the UPDATE writes new versions.
UPDATE public.ringcx_inbound AS t
SET source_seq = s.rn
FROM (SELECT ctid, row_number() OVER (ORDER BY ctid) AS rn FROM public.ringcx_inbound) AS s
WHERE t.ctid = s.ctid;

-- New rows get the next number automatically, in the order they are inserted.
CREATE SEQUENCE public.ringcx_inbound_source_seq_seq OWNED BY public.ringcx_inbound.source_seq;
SELECT setval('public.ringcx_inbound_source_seq_seq', (SELECT max(source_seq) FROM public.ringcx_inbound));
ALTER TABLE public.ringcx_inbound ALTER COLUMN source_seq SET DEFAULT nextval('public.ringcx_inbound_source_seq_seq');
ALTER TABLE public.ringcx_inbound ALTER COLUMN source_seq SET NOT NULL;
-- Optional, recommended: enforce uniqueness (creates an index). The dashboard checks uniqueness either way.
-- ALTER TABLE public.ringcx_inbound ADD CONSTRAINT ringcx_inbound_source_seq_key UNIQUE (source_seq);

-- Checks inside the transaction (expect: rows = numbered = distinct, min = 1, max = rows):
SELECT count(*) AS rows, count(source_seq) AS numbered, count(DISTINCT source_seq) AS distinct_seq,
       min(source_seq), max(source_seq) FROM public.ringcx_inbound;
-- If all hold: COMMIT;   otherwise: ROLLBACK;
-- COMMIT;

-- ===== Part 3: read-only verification after COMMIT (compare with the sheet's tallies) =====
SET default_transaction_read_only = on;
BEGIN READ ONLY;
WITH a AS (
  SELECT right(regexp_replace(split_part(ani, '.', 1), '\D', '', 'g'), 10) AS phone10,
         interaction_date::date AS day, agent_full_name AS agent, source_seq
  FROM public.ringcx_inbound
  WHERE queue = 'Two-Minute Transfers' AND call_result = 'Inbound Accepted' AND interaction_date >= DATE '2026-04-01'),
m AS (SELECT phone10, day FROM a WHERE length(phone10) = 10 GROUP BY 1, 2 HAVING count(DISTINCT agent) > 1),
first_seq AS (SELECT DISTINCT ON (phone10, day) agent FROM a JOIN m USING (phone10, day) ORDER BY phone10, day, source_seq),
last_seq AS (SELECT DISTINCT ON (phone10, day) agent FROM a JOIN m USING (phone10, day) ORDER BY phone10, day, source_seq DESC)
SELECT coalesce(f.agent, l.agent) AS agent, f.n AS first_by_seq, l.n AS last_by_seq
FROM (SELECT agent, count(*) AS n FROM first_seq GROUP BY 1) f
FULL JOIN (SELECT agent, count(*) AS n FROM last_seq GROUP BY 1) l USING (agent)
ORDER BY 1;
-- expect first_by_seq: Arturo Valenzuela 1, Austin Bush 1, Ben Hall 1, Benjamin Hall 12, Camaran Hyder 12,
--   David Morris 2, Eric Hansen 1, Ian Rawley 9, Jav Hyder 5, Kris Seiverd 2, Max Fromm 7, Nick Molina 4,
--   Noah Hanusey 2 (others 0)
ROLLBACK;

-- ===== Loader rule for future rows (no SQL here; applies to whatever populates the table) =====
-- 1. Insert rows in the same order as the source report/file (CSV line order), one report at a time,
--    oldest report first, in a single transaction per report; never run two loaders at once.
-- 2. Do not set source_seq yourself; leave it to the default so it follows insertion order.
-- 3. Never renumber source_seq, and avoid re-inserting old rows (re-inserts get new, later numbers).
-- 4. Apply the same row-skipping rule the sheet's importer uses, so both keep the same rows in the same order
--    while the sheet is still the reference.
