-- Read-only inspection of public.ringcx_inbound for the BFF dashboard (aggregates and structure only).
-- No query returns a phone number, caller name or individual row. Nothing here writes or changes anything.
--
-- pgAdmin: open a Query Tool on a read-only role. Run step 0 once, then each numbered query on its own (select it
-- and press F5 / Execute; the Query Tool shows only the last result when several run together), then ROLLBACK.
-- psql: psql -h <host> -U <readonly_user> -d <db> -v ON_ERROR_STOP=1 -f ringcx_inbound_inspection.sql -o results.txt

-- 0. Run once first. It makes this transaction read-only, and every later one in this tab or session.
--    (default_transaction_read_only alone covers only transactions that start after it, so BEGIN READ ONLY
--    is needed too.) If a query errors, run ROLLBACK; and then step 0 again.
SET default_transaction_read_only = on;
BEGIN READ ONLY;
SET LOCAL statement_timeout = '60s';
SHOW TimeZone;                       -- Q0: the session's time zone, before the next line changes it
SET LOCAL TIME ZONE 'UTC';           -- hour/date results below are in UTC for zone-aware columns

-- Q1. Columns, types and defaults
SELECT table_schema, column_name, data_type, udt_name, is_nullable, column_default, is_identity, datetime_precision
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = 'ringcx_inbound'
ORDER BY ordinal_position;

-- Q2. Keys, constraints, indexes and size
SELECT conname, contype, pg_get_constraintdef(oid) AS definition
FROM pg_constraint WHERE conrelid = 'public.ringcx_inbound'::regclass;
SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'ringcx_inbound';
SELECT count(*) AS total_rows, pg_size_pretty(pg_total_relation_size('public.ringcx_inbound')) AS size
FROM public.ringcx_inbound;

-- Q3. Queue spellings that look like Two-Minute Transfers (names and counts only)
SELECT queue, length(queue) AS chars, count(*) AS rows
FROM public.ringcx_inbound WHERE queue ILIKE '%two%minute%' GROUP BY 1, 2 ORDER BY 3 DESC;

-- Q4. Target queue: rows, coverage, time of day, missing values (from 2026-04-01)
SELECT count(*) AS rows,
       min(interaction_date) AS first_value, max(interaction_date) AS last_value,
       count(DISTINCT interaction_date::date) AS distinct_days,
       count(*) FILTER (WHERE interaction_date::timestamp <> date_trunc('day', interaction_date::timestamp)) AS rows_with_time_of_day,
       count(*) FILTER (WHERE interaction_date IS NULL) AS missing_date,
       count(*) FILTER (WHERE call_result IS NULL OR trim(call_result) = '') AS missing_result,
       count(*) FILTER (WHERE agent_full_name IS NULL OR trim(agent_full_name) = '') AS missing_agent,
       count(*) FILTER (WHERE ani IS NULL OR length(regexp_replace(split_part(ani::text, '.', 1), '\D', '', 'g')) < 10) AS missing_or_short_phone
FROM public.ringcx_inbound
WHERE queue = 'Two-Minute Transfers' AND interaction_date >= DATE '2026-04-01';

-- Q5. Target queue: rows per month and per hour of day (hour is UTC for zone-aware columns; all 0 for date columns)
SELECT to_char(date_trunc('month', interaction_date::timestamp), 'YYYY-MM') AS month, count(*) AS rows
FROM public.ringcx_inbound
WHERE queue = 'Two-Minute Transfers' AND interaction_date >= DATE '2026-04-01' GROUP BY 1 ORDER BY 1;
SELECT extract(hour FROM interaction_date::timestamp)::int AS hour, count(*) AS rows
FROM public.ringcx_inbound
WHERE queue = 'Two-Minute Transfers' AND interaction_date >= DATE '2026-04-01' GROUP BY 1 ORDER BY 1;

-- Q6. Target queue: call results, distinct agents and distinct 10-digit phones
SELECT call_result, count(*) AS rows
FROM public.ringcx_inbound
WHERE queue = 'Two-Minute Transfers' AND interaction_date >= DATE '2026-04-01' GROUP BY 1 ORDER BY 2 DESC;
SELECT count(DISTINCT agent_full_name) AS agents,
       count(DISTINCT right(regexp_replace(split_part(ani::text, '.', 1), '\D', '', 'g'), 10)) AS phones
FROM public.ringcx_inbound
WHERE queue = 'Two-Minute Transfers' AND interaction_date >= DATE '2026-04-01';

-- Q7. uii: missing, repeated, exact-duplicate rows, format (lengths, digits only, starts with the call date)
SELECT count(*) AS rows,
       count(*) FILTER (WHERE uii IS NULL OR trim(uii::text) = '') AS missing_uii,
       count(uii) - count(DISTINCT uii) AS repeated_uii_rows,
       (SELECT count(*) FROM (SELECT uii FROM public.ringcx_inbound
          WHERE queue = 'Two-Minute Transfers' AND interaction_date >= DATE '2026-04-01' AND uii IS NOT NULL
          GROUP BY uii HAVING count(*) > 1) d) AS uii_values_repeated,
       count(*) - (SELECT count(*) FROM (SELECT DISTINCT uii, interaction_date, call_result, agent_full_name, ani
          FROM public.ringcx_inbound WHERE queue = 'Two-Minute Transfers' AND interaction_date >= DATE '2026-04-01') e)
          AS exact_duplicate_rows,
       min(length(uii::text)) AS min_len, max(length(uii::text)) AS max_len,
       count(*) FILTER (WHERE uii::text ~ '^[0-9]+$') AS digits_only,
       count(*) FILTER (WHERE left(uii::text, 8) = to_char(interaction_date::timestamp, 'YYYYMMDD')) AS starts_with_call_date
FROM public.ringcx_inbound
WHERE queue = 'Two-Minute Transfers' AND interaction_date >= DATE '2026-04-01';

-- Q8. Columns that could record load or source order (names only): identities, sequences, timestamps
SELECT column_name, data_type, column_default, is_identity
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = 'ringcx_inbound'
  AND (is_identity = 'YES' OR column_default LIKE 'nextval%' OR data_type LIKE 'timestamp%'
       OR column_name ~* '(id|seq|row|order|created|loaded|imported|inserted|updated)');

-- Q9. Same-day callers with more than one accepted agent: the cases where row order decides the agent.
--     The sheet has 59 such caller-days through 2026-10-21 (raw agent names, last 10 phone digits).
WITH a AS (
  SELECT right(regexp_replace(split_part(ani::text, '.', 1), '\D', '', 'g'), 10) AS phone10,
         interaction_date::date AS day, interaction_date::timestamp AS ts, uii, agent_full_name AS agent
  FROM public.ringcx_inbound
  WHERE queue = 'Two-Minute Transfers' AND call_result = 'Inbound Accepted' AND interaction_date >= DATE '2026-04-01'),
g AS (
  SELECT phone10, day, count(*) AS calls, count(DISTINCT ts) AS distinct_times,
         count(DISTINCT uii) AS distinct_uii, count(uii) AS uii_present
  FROM a WHERE length(phone10) = 10
  GROUP BY 1, 2 HAVING count(DISTINCT agent) > 1)
SELECT count(*) AS multi_agent_caller_days,
       count(*) FILTER (WHERE day <= DATE '2026-10-21') AS through_2026_10_21,
       count(*) FILTER (WHERE distinct_times = calls) AS every_call_has_its_own_time,
       count(*) FILTER (WHERE distinct_uii = calls AND uii_present = calls) AS every_call_has_its_own_uii
FROM g;

-- Q10. In those caller-days, does ordering by time pick the same first agent as ordering by uii?
WITH a AS (
  SELECT right(regexp_replace(split_part(ani::text, '.', 1), '\D', '', 'g'), 10) AS phone10,
         interaction_date::date AS day, interaction_date::timestamp AS ts, uii, agent_full_name AS agent
  FROM public.ringcx_inbound
  WHERE queue = 'Two-Minute Transfers' AND call_result = 'Inbound Accepted' AND interaction_date >= DATE '2026-04-01'),
multi AS (SELECT phone10, day FROM a WHERE length(phone10) = 10 GROUP BY 1, 2 HAVING count(DISTINCT agent) > 1),
by_time AS (SELECT DISTINCT ON (a.phone10, a.day) a.phone10, a.day, a.agent FROM a JOIN multi USING (phone10, day)
            ORDER BY a.phone10, a.day, a.ts, a.uii),
by_uii AS (SELECT DISTINCT ON (a.phone10, a.day) a.phone10, a.day, a.agent FROM a JOIN multi USING (phone10, day)
           ORDER BY a.phone10, a.day, a.uii)
SELECT count(*) AS caller_days, count(*) FILTER (WHERE t.agent <> u.agent) AS first_agent_differs
FROM by_time t JOIN by_uii u USING (phone10, day);

-- Q11. Freshness: latest call; repeat with any load/created column found in Q8 (replace the name)
SELECT max(interaction_date) AS latest_call FROM public.ringcx_inbound WHERE queue = 'Two-Minute Transfers';
-- SELECT max(<load_column>) AS latest_load FROM public.ringcx_inbound;

-- End: closes the read-only transaction; nothing was changed.
ROLLBACK;
