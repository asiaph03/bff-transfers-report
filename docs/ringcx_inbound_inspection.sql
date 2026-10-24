-- Read-only inspection of public.ringcx_inbound for the BFF dashboard (aggregates and structure only).
-- No query returns a phone number, caller name or individual row. Nothing here writes or changes anything.
--
-- pgAdmin: open a Query Tool on a read-only role. Run step 0 once, then each numbered query on its own (select it
-- and press F5 / Execute; the Query Tool shows only the last result when several run together), then ROLLBACK.
-- If a query errors: run ROLLBACK; then step 0 again, and continue with the next query.
-- psql: psql -h <host> -U <readonly_user> -d <db> -v ON_ERROR_STOP=1 -f ringcx_inbound_inspection.sql -o results.txt
--
-- Q1 must come first. Q2 onwards assume interaction_date is date, timestamp or timestamptz. If Q1 shows a text
-- type (text, character varying), stop after Q1 and share it: the date queries need a different form.

-- 0. Run once first. It makes this transaction read-only, and every later one in this tab or session.
--    (default_transaction_read_only alone covers only transactions that start after it, so BEGIN READ ONLY
--    is needed too.)
SET default_transaction_read_only = on;
BEGIN READ ONLY;
SET LOCAL statement_timeout = '60s';
SHOW TimeZone;                       -- Q0: the session's time zone, before the next line changes it
SET LOCAL TIME ZONE 'UTC';           -- hour/date results below are in UTC for zone-aware columns

-- ===== Priority 1: interaction_date type and time-zone behaviour =====

-- Q1. Column types (all columns; no data)
SELECT column_name, data_type, udt_name, is_nullable, column_default, is_identity
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = 'ringcx_inbound'
ORDER BY ordinal_position;

-- Q2. Does interaction_date carry a time of day, and in which hours? (hours in UTC for timestamptz;
--     all hour 0 means a plain date)
SELECT extract(hour FROM interaction_date::timestamp)::int AS hour, count(*) AS rows
FROM public.ringcx_inbound
WHERE queue = 'Two-Minute Transfers' AND interaction_date >= DATE '2026-04-01'
GROUP BY 1 ORDER BY 1;

-- ===== Priority 2: target-queue rows and coverage =====

-- Q3. Queue spellings that look like Two-Minute Transfers (labels and counts only)
SELECT queue, length(queue) AS chars, count(*) AS rows
FROM public.ringcx_inbound WHERE queue ILIKE '%two%minute%' GROUP BY 1, 2 ORDER BY 3 DESC;

-- Q4. Rows, first and last date, distinct days, rows before the 2026-04-01 cutoff
SELECT count(*) AS queue_rows_all_dates,
       count(*) FILTER (WHERE interaction_date >= DATE '2026-04-01') AS rows_from_2026_04_01,
       count(*) FILTER (WHERE interaction_date < DATE '2026-04-01') AS rows_before_cutoff,
       min(interaction_date) AS first_value, max(interaction_date) AS last_value,
       count(DISTINCT interaction_date::date) AS distinct_days
FROM public.ringcx_inbound WHERE queue = 'Two-Minute Transfers';

-- Q5. Rows per month from the cutoff (compare with the sheet: Apr 261, May 75, Jun 4, Jul 281, Aug 459, Sep 409,
--     Oct 197 through Oct 21)
SELECT to_char(date_trunc('month', interaction_date::timestamp), 'YYYY-MM') AS month, count(*) AS rows
FROM public.ringcx_inbound
WHERE queue = 'Two-Minute Transfers' AND interaction_date >= DATE '2026-04-01'
GROUP BY 1 ORDER BY 1;

-- ===== Priority 3: missing required fields =====

-- Q6. Missing values in the target queue (all dates, so rows with no date are counted too)
SELECT count(*) FILTER (WHERE interaction_date IS NULL) AS missing_date,
       count(*) FILTER (WHERE call_result IS NULL OR trim(call_result) = '') AS missing_result,
       count(*) FILTER (WHERE agent_full_name IS NULL OR trim(agent_full_name) = '') AS missing_agent,
       count(*) FILTER (WHERE call_result = 'Inbound Accepted'
                        AND (agent_full_name IS NULL OR trim(agent_full_name) = '')) AS accepted_missing_agent,
       count(*) FILTER (WHERE ani IS NULL
                        OR length(regexp_replace(split_part(ani::text, '.', 1), '\D', '', 'g')) < 10) AS missing_or_short_phone
FROM public.ringcx_inbound WHERE queue = 'Two-Minute Transfers';

-- ===== Priority 4: uii nulls, duplicates and format =====

-- Q7. uii (counts and lengths only)
SELECT count(*) AS rows,
       count(*) FILTER (WHERE uii IS NULL OR trim(uii::text) = '') AS missing_uii,
       count(uii) - count(DISTINCT uii) AS repeated_uii_rows,
       count(*) - count(DISTINCT (uii, interaction_date, call_result, agent_full_name, ani)) AS exact_duplicate_rows,
       min(length(uii::text)) AS min_len, max(length(uii::text)) AS max_len,
       count(*) FILTER (WHERE uii::text ~ '^[0-9]+$') AS digits_only,
       count(*) FILTER (WHERE left(uii::text, 8) = to_char(interaction_date::timestamp, 'YYYYMMDD')) AS starts_with_call_date
FROM public.ringcx_inbound
WHERE queue = 'Two-Minute Transfers' AND interaction_date >= DATE '2026-04-01';

-- ===== Priority 5: same-day callers with more than one accepted agent =====

-- Q8. The cases where row order decides the agent. The sheet has 59 such caller-days through 2026-10-21
--     (raw agent names, last 10 phone digits).
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

-- Q9. In those caller-days, does ordering by time pick the same first agent as ordering by uii?
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

-- ===== Priority 6: a source-order or load-sequence column =====

-- Q10. Columns that could record load or source order (names only)
SELECT column_name, data_type, column_default, is_identity
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = 'ringcx_inbound'
  AND (is_identity = 'YES' OR column_default LIKE 'nextval%' OR data_type LIKE 'timestamp%'
       OR column_name ~* '(id|seq|row|order|created|loaded|imported|inserted|updated)');

-- ===== Optional, later =====

-- Q11. Keys, indexes and size
SELECT conname, contype, pg_get_constraintdef(oid) AS definition
FROM pg_constraint WHERE conrelid = 'public.ringcx_inbound'::regclass;
SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'ringcx_inbound';
SELECT count(*) AS total_rows, pg_size_pretty(pg_total_relation_size('public.ringcx_inbound')) AS size
FROM public.ringcx_inbound;

-- Q12. Call results, distinct agents and phones (sheet through Oct 21: 21 agents, 1,329 phones,
--      Inbound Accepted 1,258, Inbound Abandoned 376)
SELECT call_result, count(*) AS rows
FROM public.ringcx_inbound
WHERE queue = 'Two-Minute Transfers' AND interaction_date >= DATE '2026-04-01' GROUP BY 1 ORDER BY 2 DESC;
SELECT count(DISTINCT agent_full_name) AS agents,
       count(DISTINCT right(regexp_replace(split_part(ani::text, '.', 1), '\D', '', 'g'), 10)) AS phones
FROM public.ringcx_inbound
WHERE queue = 'Two-Minute Transfers' AND interaction_date >= DATE '2026-04-01';

-- End: closes the read-only transaction; nothing was changed.
ROLLBACK;
