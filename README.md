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

Only manually started runs (`workflow_dispatch`) of this one repository can get a token, and the token
is read-only for Sheets and lasts 15 minutes.

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

### With Claude

Say **"Refresh the BFF Transfers dashboard now."** Claude then:

1. Confirms the repository (`asiaph03/bff-transfers-report`) and branch (`main`).
2. Checks that it can start workflows (GitHub CLI signed in on your computer) and that the two Google variables exist.
3. Runs `gh workflow run refresh.yml -f mode=publish` and waits for it (`gh run watch`).
4. Reads the job results and the validation report.
5. Loads the live `summary.json` and checks its `refreshed_at` matches the run.
6. Reports the refresh time, or what failed. It never says the dashboard was updated without that check.

Claude needs the GitHub CLI signed in on your computer (`gh auth login`, done by you) with access to this
repository. There is no Google key to handle.

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
| `GOOGLE_ACCESS_TOKEN is not set` / Google sign-in step failed | Check the two repository variables (setup step 2) and the Workload Identity setup (step 1.4) |
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
The Google Sheets stay private and are read with a 15-minute read-only token; no Google key exists.
