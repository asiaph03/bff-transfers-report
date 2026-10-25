# Publication safety: a failed refresh never replaces the last-known-good report

The live report is whatever the last successful GitHub Pages deployment contains. Nothing the workflow does before the
deployment step can change it, and three checks make sure that a bad, incomplete or older report normally never gets to that
step (one narrow exception is described under "Known limitation"). This page describes what runs, in what order, what each failure
leaves behind, and how to recover.

## The sequence (`.github/workflows/refresh.yml`)

```
Refresh dashboard (manual only; one run at a time, see "Queueing")
  build job (time limit 70 minutes, see "Time limits")
    1  rule tests (pytest -q pipeline) and config check          fail: stop, nothing built
    2  build: read sources, validate, write out/site, then READ THE FILES BACK and verify them     fail: exit 1, out/site removed
    3  verify the built site again, as a separate step (pipeline/verify_site.py)                    fail: stop, nothing uploaded
         outputs: refreshed_at, version, data_bytes, rows of the candidate report
    4  upload site-preview (the recovery copy)                    kept 90 days for publish runs, 14 for previews
    5  upload the Pages artifact (publish mode only)              kept 1 day (the action's default)
  deploy job (publish mode only, needs the build job, time limit 25 minutes)
    6  refuse to replace a newer live report (pipeline/live_check.py guard)                        fail: stop, nothing deployed
    7  deploy-pages
    8  verify the live site serves the candidate report (live_check verify, up to about 12 minutes) fail: job red, see below
```

Steps 4 to 8 run only if every earlier step succeeded: none of the gating steps uses `always()`, `failure()` or `continue-on-error`, and
the deploy job needs a successful build job (all checked by `pipeline/test_workflow.py`).

## What stops a bad report

| Check | Where | Stops |
|---|---|---|
| Existing validation (sources, rules, baseline limits, privacy, vendor checksum) | `pipeline/validate.py`, `build.py` | unchanged |
| **Site verification** | `build.py` after writing; its own workflow step | a site that is not exactly: the five expected files, a manifest whose version, byte count and row count describe the bytes of `data.json`, matching timestamps (not in the future), only the approved public fields, no phone numbers, emails or secrets, the page and chart library as in the repository |
| **Stale-deployment guard** | deploy job, before `deploy-pages` | a candidate older than the live report; an unreadable or untrusted live report (unless the run was started as the first release) |
| **Live check** | deploy job, after `deploy-pages` | nothing: it reports. See below |

Verification messages name files, fields and counts, never values from the report.

## Time limits

The build job stops at 70 minutes. The pipeline gives itself 40 minutes of Google reads (`GoogleSheetsSource.JOB_BUDGET`); the full test suite
takes about 9 minutes on a laptop (not yet measured on the runner, 15 minutes are allowed); installing, verifying and uploading take a few
more. Recorded runs of the earlier workflow (which ran fewer tests) took 2.7 to 17.2 minutes in total, the Google step 2.4 to 16.9. A test in
`pipeline/test_workflow.py` fails if the limit is ever lower than the Google budget plus that allowance. Revisit the limit after the first
real run of this workflow. The deploy job stops at 25 minutes: the guard, the deployment and up to about 12.7 minutes of live checking.
A timeout fails the job, so nothing is deployed after it.

## Queueing

All runs share one concurrency group, so only one runs at a time and a running run is never cancelled. GitHub keeps at most one pending run in
a group and replaces an older pending run with a newer one (documented behavior), so an extra dispatch queued behind a running one can be
dropped without running. Each run waits for its own live check before the next starts.

## Cleanup rules

The build deletes only the files it writes (`index.html`, `data.json`, `summary.json`, `.nojekyll`, `vendor/d3.v7.8.5.min.js`) and the
`vendor` folder if that leaves it empty. Anything else found in `out/site` is left untouched and the build stops with a message. Files
elsewhere under `out/` (the validation report, for example) are not touched, and links are never followed.

Cleanup on an error or an interrupt is best effort. A file that cannot be deleted, or any other problem in cleanup, is logged as a warning and
never replaces the failure being handled: the original exception or interrupt is what comes out, the exit stays non-zero, and nothing is
uploaded from a failed run. If the validation report itself cannot be written, the run still fails and names the original cause.

Signals: cleanup runs when the build handles an error or an interrupt (SIGINT, which Python raises as `KeyboardInterrupt`). When a run is
cancelled, GitHub's runner sends SIGINT to the step's process first, then SIGTERM after 7.5 seconds, and then ends the process tree
(documented behavior). Whether Python receives that SIGINT through the step's shell was not verified. SIGTERM, a kill, a job timeout and a
power loss end the process without cleanup, so a partial `out/site` can remain on that runner. It is never uploaded (the steps that upload
run only after success) and the runner is discarded.

## What each failure leaves live

| Failure | Result |
|---|---|
| Google Sheets unreadable, validation or baseline failure, site verification failure, an unexpected error | Nothing is uploaded or deployed. The live report is unchanged. |
| Interrupt, cancel, timeout, kill | A partial `out/site` may be left on the runner (cleanup is not guaranteed on a kill), and the step does not succeed, so no upload or deployment follows. The live report is unchanged. |
| Stale guard refuses | Nothing is deployed. The live report is unchanged. |
| `deploy-pages` fails | Expected to leave the previous deployment active (GitHub's behavior; not observed here, see the end). |
| Live check fails after a deployment | The deployment **may already have happened**. The check does not roll anything back. |

## The stale-deployment guard

The build job exposes the candidate's `refreshed_at`. Before deploying, the deploy job reads the live `summary.json` and compares:

- candidate newer than live: allowed;
- candidate with the same timestamp: allowed (the same report again, for example after a failed deployment);
- candidate older than live: refused, always;
- live report missing, not JSON, without a valid time, or dated in the future: refused, because the comparison cannot be trusted. Only a run
  started with `first_release` skips this, and it still refuses if the live report is newer.

The live report is read through the CDN, which can serve a copy up to about 10 minutes old. Runs are serialized and each waits for its live
check, so a run normally sees its predecessor's report, and a stale view can only make the guard more permissive, never stricter.

**Known limitation (not fixed).** The guard trusts the CDN's view. If an older run's deploy job is re-run within roughly 10 minutes of a newer
deployment, and that older run's Pages artifact still exists, a stale copy of `summary.json` can show a report older than the re-run candidate.
The guard then allows it and the newer report is replaced. A re-run hours later, or once the CDN shows the newer report, is refused.
Operating rule: **never re-run the deploy job of a run that is not the newest successful publish run.** A check that does not depend on the CDN
(comparing run numbers through the Actions API) was considered; it needs a new job permission and is not part of this phase.
`pipeline/test_live_check.py` pins this behavior so it cannot change unnoticed.

## The live check

After `deploy-pages` it asks the live site, every 30 seconds for up to about 12 minutes, whether it serves the candidate:
`summary.json` with the candidate's timestamp and version, and a `data.json` whose own SHA-256 matches the manifest, with the
manifest's size, row count and timestamp. Cached older files, missing files and a new summary beside old data are waited out. A
newer report already live, or the same timestamp with a different version, stops it at once. It does not check `index.html` or the chart
library, nor the parts of `summary.json` other than its time and version.

If it fails it says so, in these words: **the deployment may already have happened; this check does not roll anything back.** Look at
`summary.json` and `data.json` on the site, and compare with the run summary's "Candidate report" (version and time). It checks what its
own requests saw; other visitors' caches can lag.

## Recovery artifact and procedure

Every successful run uploads `site-preview` with exactly the verified files. For publish runs it is kept 90 days (14 for previews), subject
to any shorter repository or organization retention limit (not verified for this repository). The Pages artifact that a re-run of the deploy
job needs is kept for 1 day only (the default of `actions/upload-pages-artifact`, which this workflow does not change). The run summary lists the
candidate's `refreshed_at` and `version` so the right copy can be found. None of the steps below deploys or changes anything.

1. **Nothing to do** after a failure before deployment: the live report was never touched.
2. **Bad but valid report is live: roll forward.** Fix the cause (source data or an approved config change) and run **Refresh dashboard**
   with `mode: publish`. A new report is newer, so the guard allows it, and verification and the live check run as usual.
3. **Find and check the last known good copy** (to compare, investigate or keep). Verification compares the files with the repository's page,
   chart-library checksum and approved fields **as of the run's commit**, so it must be run from that commit:
   1. *Pick the run.* Actions, **Refresh dashboard**: the newest publish run whose deploy job and live check were green, and that is not the
      run of the bad report. Open it and note, from its summary, `refreshed_at`, `version`, `rows` and `data.json bytes` ("Candidate report
      (verified)"). Note its commit: the SHA on the run page, or `head_sha` from
      `https://api.github.com/repos/<owner>/<repo>/actions/runs/<run id>`.
   2. *Get that commit in a separate checkout, never in your working copy:*
      ```
      git clone https://github.com/<owner>/<repo>.git recovery-check
      cd recovery-check
      git checkout <head_sha>
      ```
   3. *Install what that commit needs* (the workflow uses Python 3.12):
      ```
      python -m venv .venv
      .venv/bin/pip install -r pipeline/requirements.txt          # Windows: .venv\Scripts\pip
      ```
   4. *Download the artifact.* On the run page, under Artifacts, download `site-preview` and unzip it into an empty folder outside the checkout.
   5. *Verify it from the checkout:*
      ```
      .venv/bin/python -m pipeline.verify_site <unzipped folder>  # Windows: .venv\Scripts\python
      ```
      It must print `site verified: 5 files, ...` with the same `refreshed_at` and `version` as the run summary. If it reports that the page,
      fields or checksum differ, the checkout is not the run's commit. If that commit no longer exists, the same command with `--source-index`
      and `--config` pointing at files you trust is a weaker check (the page comparison becomes trivial); say so when reporting.
   6. *Optionally compare with what is live now* (read-only requests):
      ```
      .venv/bin/python -m pipeline.live_check verify --url https://<owner>.github.io/<repo>/ --refreshed-at <time> --version <version> \
        --data-bytes <bytes> --rows <rows> --deadline 1
      ```
      `LIVE CHECK PASSED` means the live site serves exactly that report; a failure only tells you it does not (its warning about a deployment
      refers to the deploy job and does not apply when you run it by hand).
4. **Redeploy the same newest report** (a deployment or live check failed): on that run, choose **Re-run failed jobs**. The guard allows the same
   timestamp. This works only while that run's Pages artifact still exists (1 day); after that, roll forward (step 2). Do this only for the
   newest successful publish run (see the known limitation).
5. **Restore an exact older report: not automated, on purpose.** The guard refuses an older report over a newer one, and this change does not
   weaken it. There is no rollback mode in this phase. If one is ever needed it should be a separate, reviewed change that deploys a verified
   recovery copy and records who asked and why.

The steps are exercised against scratch folders and a local stand-in for the site only (`pipeline/test_live_check.py`, the simulated deployment
and recovery-drill tests). No real deployment or rollback was performed.

## Verified and assumed

Verified by tests and probes here: every pre-deployment failure leaves the published files untouched and nothing to upload; the
verification catches truncated, tampered, inconsistent, extra, missing and sensitive output; cleanup failures never replace the original error
or interrupt and never make a failed run succeed; the guard and live check decisions, including cache lag and mismatches; the workflow's order,
outputs, gating, time limits, concurrency and absence of schedules (parsed, not run). Checked against GitHub's documentation and action
definition: the Pages artifact's default retention (1 day), that a newer pending run replaces an older pending one, and the cancellation signal order.

Assumed, not verified: GitHub Pages switches deployments atomically and a failed `deploy-pages` leaves the previous one active; the CDN's timing
after a deploy (copies seen up to about 10 minutes old); the artifact retention limits that apply to this repository; that Python receives the
runner's SIGINT through the step's shell; the behavior of re-running only the deploy job; the test suite's duration on the runner; and the
workflow itself, which was not run.
