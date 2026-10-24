# BFF Transfers report

Public dashboard of the last 300 paid Two Minute Transfers leads:
https://asiaph03.github.io/bff-transfers-report/

The data refreshes **only when someone asks for it**. Nothing runs on a schedule, and
editing a Google Sheet, the config or the code never publishes new numbers by itself.

## How it works

```
Google Sheets (5, read-only)
   │  GitHub Actions: "Refresh dashboard" (run by hand)
   ▼
pipeline/build.py ── config/report.yaml (all rules)
   │  tests → fetch → calculate → validate
   ▼
out/site/ (index.html + data.json + summary.json)  ──►  GitHub Pages (same URL)
```

| Path | What it is |
|---|---|
| `config/report.yaml` | Spreadsheet IDs, tabs, columns and every business rule |
| `pipeline/sheets.py` | Reads the sheets (read-only scope) |
| `pipeline/rules.py` | The rules: paid leads, agent match, lender closes |
| `pipeline/validate.py` | Checks that must pass before publishing |
| `pipeline/build.py` | Runs everything and writes `out/` |
| `pipeline/test_rules.py` | Tests on made-up data |
| `site/index.html` | The dashboard page; it loads `data.json` and `summary.json` |
| `.github/workflows/refresh.yml` | The manual refresh (preview or publish) |
| `.github/workflows/test.yml` | Tests on every code or config push; never fetches or deploys |

Generated data is never committed. It exists only inside the workflow run and the published site.

### Sources

| Source | Tab | Used for |
|---|---|---|
| Ringba Call Tracker | Ringba2 | Paid calls (`buyerConversionAmount` > 0) |
| RingCentral Call Logs 2026 | Two Minute Transfers | Agent who answered |
| Citizen | Affiliate_Clients | Closes, debt, submitted date |
| Forthcrm - Cordoba | Contacts | Closes, enrolled debt, submitted date (home or cell phone) |
| Forthcrm - DebtBlue | ContactsV2 | Closes by stage and created date (no debt field) |

### RingCX from PostgreSQL (prepared, opt-in, not switched on)

Google Sheets stays the RingCX source (`sources.ringcx.backend: sheets`). `pipeline/pg.py` can read the
same calls from `public.ringcx_inbound`, a table maintained outside this project. The pipeline only reads it:

- read-only session in one REPEATABLE READ snapshot, statement timeout, row cap, rows read must equal the
  snapshot count; column names come only from the config, values are bound parameters;
- errors report the exception type and SQLSTATE only (no connection string, host or values);
- rows come back under the sheet's column names, so every reporting rule runs unchanged;
- data-quality checks fail the run on a missing interaction date or call result, or an accepted call
  without an agent or phone; missing or repeated `uii` values are warnings (counts only).

Mapping (from the owner's pgAdmin inspection): `interaction_date` = date, `call_result` = result,
`agent_full_name` = agent, `ani` = phone, `uii` = candidate call id, `queue` = queue (filter
`Two-Minute Transfers`), from 2026-04-01. `agent_email` is not used by any rule.

**Blocker (owner decision, Oct 24: keep rule U2 and Google Sheets):** the database has the same calls and
totals as the sheet, but no column that records ingestion order, which U2 needs for 59 same-day multi-agent
cases (agent shown for up to 11 of 300 leads). Read-only checks found that physical insertion order (`ctid`)
reproduces the sheet's order (first- and last-agent tallies match for all 18 agents; the table has had 0
updates and 0 deletes), but `ctid` is not durable. The fix is the owner's: capture it once into a real column
`source_seq` and keep it for new rows (`docs/ringcx_inbound_source_seq.sql`). The config already orders by
`interaction_date, source_seq` and fails closed until that column exists and is present and unique on every
row. Time order (`enqueue_start_time`) and `uii` order do not reproduce U2 and are not used.

**Comparison run (preview only, nothing published):** Actions -> Refresh dashboard -> `mode: preview`,
`ringcx_source: postgres-compare`. Needs the repository secret `RINGCX_DATABASE_URL` (read-only role) and a
network path from GitHub Actions to the database. The run reads both sources and **fails** if, over the
days both snapshots cover, any call is in one source only, call-result totals differ, a same-day tie is
settled differently, or any selected lead's answered/agent result differs. Calls after the earlier
snapshot's last day are counted as snapshot timing, not compared. A publish run refuses the database
source until `approved_for_dashboard: true`.

`docs/ringcx_inbound_inspection.sql` is a read-only, aggregates-only inspection script.

### Rules (all in `config/report.yaml`)

- Phones match on their last 10 digits.
- Paid lead: payout above $0. Each phone keeps its latest paid call; the newest 300 of those are the report.
  Ties on call time go to `inboundCallId`. Fewer than 300 fails the run.
- Agent: the first `Inbound Accepted` RingCX call on or after the paid-call date; same-day ties go to the
  first row in sheet order. Ben Hall = Benjamin Hall. Leads paid before 1 Apr 2026 (RingCX log start) show
  "Before RingCX log".
- Close: a lender record whose status is on that lender's own Closed-tab list, dated on or after the paid-call
  date (calendar date only). Each lead counts once, at its earliest close.
- Active: closed and not Cancelled, Returned, Cancellation Review, File Returned or Dropped / Cancelled.
- A status that is neither closing nor a known non-closing status fails the run until it is reviewed.

### Answering agent and closing agent

- `agent` is the **answering agent** (the RingCX rule above). It drives the funnel and call metrics for every
  lead, closed or not, and is never replaced by a closer.
- `closing_agent` is a separate field for closed deals only. Source of truth: the ownership fields of the lender
  CRM record the deal was matched to (Citizen: Assignees, its assigned-agent field; Cordoba: Assigned To;
  DebtBlue: Assigned To when Assigned Company is Better Future Finance). RingCX and the SI tab are never used to
  pick a closer. System accounts, lender staff, blanks, conflicts and names without an approved display name
  show "Closer unknown".
- Not enabled yet (`closing_agent.source: null`, `lender_values_approved: false`, `name_map: null`), so every
  closed deal is "Closer unknown" and `closing_agent` is not published. `--closer-preview` reads the ownership
  fields and writes per-lender counts to `out/closing_agent_preview.md` (outside `site/`, never published; no
  phones or deal rows), using the proposed name map. Details: `docs/closing_agent_lender_mapping.md`.
- Enabling it needs: approved fields and non-BFF values (`lender_values_approved: true`), an approved
  `name_map`, `source: lender`, and `closing_agent` added to `publish.fields` (the config check enforces this).

## One-time setup

### 1. Google service account (read-only, no key)

Key files are disabled on this Google Cloud organization (policy `iam.disableServiceAccountKeyCreation`), and
none is needed: GitHub Actions signs in to Google with a short-lived token through Workload Identity Federation.

1. Project `bff-transfers-report`, with the **Google Sheets API** enabled.
2. Service account `dashboard-reader@bff-transfers-report.iam.gserviceaccount.com`, with no project roles.
3. Share each of the five spreadsheets with that email as **Viewer** (untick "Notify").
4. In Google Cloud Shell (the `>_` icon in the console), run:

```
PROJECT_ID=bff-transfers-report
REPO=asiaph03/bff-transfers-report
SA=dashboard-reader@$PROJECT_ID.iam.gserviceaccount.com
PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')
gcloud services enable iamcredentials.googleapis.com sts.googleapis.com --project $PROJECT_ID
gcloud iam workload-identity-pools create github --project $PROJECT_ID --location global --display-name "GitHub Actions"
gcloud iam workload-identity-pools providers create-oidc bff-repo --project $PROJECT_ID --location global \
  --workload-identity-pool github --display-name "bff-transfers-report" \
  --issuer-uri https://token.actions.githubusercontent.com \
  --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.event_name=assertion.event_name" \
  --attribute-condition "assertion.repository=='$REPO' && assertion.event_name=='workflow_dispatch'"
gcloud iam service-accounts add-iam-policy-binding $SA --project $PROJECT_ID \
  --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/attribute.repository/$REPO"
echo "GCP_WIF_PROVIDER = projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/providers/bff-repo"
```

Only manually started runs (`workflow_dispatch`) of this one repository can get a token. The token is
read-only for Sheets, short-lived, and refreshed automatically by `google-auth` during a long run.

### 2. GitHub repository variables (not secrets; nothing here is a password)

Repository → Settings → Secrets and variables → Actions → **Variables** tab → New repository variable:

- `GCP_WIF_PROVIDER`: the line the last command printed (after `=`)
- `GCP_SERVICE_ACCOUNT`: `dashboard-reader@bff-transfers-report.iam.gserviceaccount.com`

### 3. GitHub Pages source (only when the first release is approved)

Settings → Pages → Build and deployment → Source: **GitHub Actions**. The URL does not change.
Until this is switched, the old `index.html` in the repository root stays live.

## Refreshing the dashboard

### From GitHub

Actions → **Refresh dashboard** → Run workflow:

- `mode: preview` builds and validates only. Download `site-preview` and `validation-report` from the run.
- `mode: publish` does the same and, if every check passes, deploys to the live URL.
- `first_release` is ticked only once, for the first approved publication, because no published baseline
  exists yet. It cannot override a failed check, and the run fails if a baseline already exists.

### With Claude ("Refresh the BFF Transfers dashboard now.")

Manual only: nothing runs on a schedule or on a code, config or data change. Claude:

1. **Repository:** confirms `asiaph03/bff-transfers-report`, the branch the run will use (`main` for a
   publish), that it matches GitHub, and that the working tree is clean.
2. **Configuration and credentials, without revealing them:** `python -m pipeline.build --check-config`;
   that the Google variables `GCP_WIF_PROVIDER` and `GCP_SERVICE_ACCOUNT` exist; and, only for a
   PostgreSQL comparison, that the `RINGCX_DATABASE_URL` secret exists (name only).
3. **Preview first:** starts (or asks you to start) Refresh dashboard with `mode: preview`. The run fetches
   all sources read-only.
4. **Source completeness:** row minimums, required columns, the lenders' Closed-tab cross-check and, for
   the database, the snapshot count and data-quality checks.
5. **Rules and output:** 300 unique paid leads, agent and close rules, approved fields only, totals,
   privacy scan and the baseline limits.
6. **Report only if every check passes:** otherwise no site is built and the live page is untouched.
7. **Tells you:** the run id, sources read, row counts, what changed against the live totals, and warnings.
8. **Publishes only after your explicit approval**, with `mode: publish` from `main`
   (never `first_release` again: a baseline exists), then confirms the live `summary.json` matches the run.

## What is checked before publishing

- Config is valid; all five sheets and required columns are present and not duplicated.
- Row counts are above minimums; paid-call times are readable.
- No unexpected lender statuses; each lender's closed-status rows equal its own Closed tab.
- Exactly 300 unique paid leads, each with a payout above $0.
- Agent matches are accepted calls on or after the paid date; closes are on or after the paid date;
  no lead is counted twice; active matches the definition.
- Published rows hold exactly the approved fields; totals reconcile; no phone numbers, emails or keys
  in any published file.
- Totals are compared with the live `summary.json` (the baseline). A move larger than both limits in
  `validation.baseline.metrics` fails the run and needs investigation.

If anything fails, nothing is deployed and the previous release stays live. The reasons are in the run's
summary and in the `validation-report` artifact. Warnings (for example months with no Ringba calls) do not
block publishing and are shown at the bottom of the page.

## Changing a rule or filter

1. Edit `config/report.yaml` (for example `report.window_size` or a status list) and commit.
2. The **Tests** workflow checks the config. It does not publish.
3. Run **Refresh dashboard** with `mode: preview`, review, then `mode: publish`.
   A large intended change will trip the baseline limits; raise the limit in the same commit and note why.

## Local testing

```
pip install -r pipeline/requirements.txt
python -m pytest -q pipeline/test_rules.py
python -m pipeline.build --check-config
```

To build from exported copies on your own machine, list them in a JSON file kept outside the repository
(`{"ringba": "path/Ringba Call Tracker.xlsx", "ringcx": "path/Two Minute Transfers.csv", ...}`) and run
`python -m pipeline.build --out out --local-files that.json --preview`. Those files contain client data: do
not commit them.

## Troubleshooting and recovery

| Message | Fix |
|---|---|
| `Google credentials not found` / Google sign-in step failed | Check the two repository variables (setup step 2) and the Workload Identity setup (step 1.4) |
| `Google Sheets still unavailable ...` (HTTP 503/500/429) | Google could not serve that spreadsheet in time. Each request is logged with its status and duration. The RingCentral Call Logs file takes about 3 minutes per request; run again later. Nothing is published |
| `Google refused the sign-in (401) even after refreshing it` or `could not be refreshed` | The Workload Identity setup or repository variables changed; check setup steps 1.4 and 2 |
| `RINGCX_DATABASE_URL is not set` / `could not connect` / `query failed (QueryCanceled…)` | Database comparison only: check the secret, the network path and the 60 s timeout; the sheet path is unaffected |
| `ringcx database matches the sheet…` / `…same-day agent ties…` failed | Investigate the named difference; do not switch sources or loosen checks until it is explained |
| `permission denied: share the spreadsheet with … as Viewer` | Share that sheet with the service-account email |
| `missing column(s)` / `used more than once` | A sheet's headers changed; update `columns` in the config after checking the change |
| `unexpected lender statuses` | Decide whether the new status closes a deal; add it to `closed_statuses` or `known_non_closing_statuses` |
| `closed-status rows match the lender's Closed tab` failed | A lender's Closed tab and the config's status list disagree; compare them |
| `only N unique paid leads` | Fewer than 300 paid leads; check the Ringba sheet |
| `change within limits` failed | Investigate the move; publish only after it is understood |

To roll back, re-run an earlier successful **Refresh dashboard** run (Actions → that run → Re-run jobs);
it rebuilds from the sheets as they are now, so for an exact earlier version use the `site-preview`
artifact it produced.

## Privacy

The dashboard is public. It publishes agent names, lender agent names, close dates, statuses, payouts and
debt amounts per deal, with lead ids (P001…) instead of phones. It never publishes phone numbers, client
names or emails, and the build fails if a published file contains a phone-number-like value or an email.
The Google Sheets stay private and are read with a short-lived, read-only token that is refreshed as needed; no Google key exists.
