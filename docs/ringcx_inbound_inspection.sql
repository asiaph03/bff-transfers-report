-- Read-only inspection of ringcx_inbound for the BFF dashboard switch.
-- Every statement only reads, and returns structure or aggregates: no phone numbers, names or rows.
-- Run as a read-only role (psql, pgAdmin or DBeaver) and share the results.
BEGIN READ ONLY;
SET LOCAL statement_timeout = '60s';

-- 1. Columns and types
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'ringcx_inbound'
ORDER BY ordinal_position;

-- 2. Keys, constraints and indexes
SELECT conname, contype, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'ringcx_inbound'::regclass;
SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'ringcx_inbound';

-- 3. Size
SELECT count(*) AS rows, pg_size_pretty(pg_total_relation_size('ringcx_inbound')) AS size FROM ringcx_inbound;

-- Replace the placeholders <date_col>, <queue_col>, <result_col>, <agent_col>, <id_col>, <phone_col>
-- with the real column names from step 1 before running steps 4 to 8.

-- 4. Date coverage and rows per month
-- SELECT min(<date_col>), max(<date_col>) FROM ringcx_inbound;
-- SELECT date_trunc('month', <date_col>) AS month, count(*) FROM ringcx_inbound GROUP BY 1 ORDER BY 1;

-- 5. Queues / campaigns (names only, with counts)
-- SELECT <queue_col>, count(*) FROM ringcx_inbound GROUP BY 1 ORDER BY 2 DESC;

-- 6. For the Two-Minute Transfers queue since 2026-04-01: results, agents count, phones count, months
-- SELECT <result_col>, count(*) FROM ringcx_inbound WHERE <queue_col> = 'Two-Minute Transfers' AND <date_col> >= '2026-04-01' GROUP BY 1 ORDER BY 2 DESC;
-- SELECT count(DISTINCT <agent_col>) AS agents, count(DISTINCT right(regexp_replace(<phone_col>::text, '\D', '', 'g'), 10)) AS phones
--   FROM ringcx_inbound WHERE <queue_col> = 'Two-Minute Transfers' AND <date_col> >= '2026-04-01';
-- SELECT date_trunc('month', <date_col>) AS month, count(*) FROM ringcx_inbound
--   WHERE <queue_col> = 'Two-Minute Transfers' AND <date_col> >= '2026-04-01' GROUP BY 1 ORDER BY 1;

-- 7. Duplicate call ids
-- SELECT count(*) - count(DISTINCT <id_col>) AS repeated_ids, count(*) FILTER (WHERE <id_col> IS NULL) AS missing_ids FROM ringcx_inbound;

-- 8. How fresh it is and how it is loaded (latest load / call times)
-- SELECT max(<date_col>) AS latest_call FROM ringcx_inbound;
-- If the table has a loaded_at / created_at / updated_at column, also: SELECT max(that_column) FROM ringcx_inbound;

-- 9. Row order for the same-day agent tie-break (counts only)
-- 9a. Does interaction_date carry a time of day? (timestamp types only)
-- SELECT count(*) FILTER (WHERE interaction_date::time <> '00:00') AS with_time, count(*) AS total
--   FROM public.ringcx_inbound WHERE queue = 'Two-Minute Transfers' AND interaction_date >= '2026-04-01';
-- 9b. Same-day accepted calls from one caller that share the same value of a candidate order column
--     (run once per candidate, e.g. uii); 0 means that column orders every tie
-- SELECT count(*) AS unresolved_ties FROM (
--   SELECT ani, interaction_date::date, uii, count(*) FROM public.ringcx_inbound
--   WHERE queue = 'Two-Minute Transfers' AND call_result = 'Inbound Accepted' AND interaction_date >= '2026-04-01'
--   GROUP BY 1, 2, 3 HAVING count(*) > 1) t;

ROLLBACK;
