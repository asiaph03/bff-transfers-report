# Dashboard update check (version polling)

The dashboard notices a newly published report and shows it without a manual reload. This is a static-hosting design:
no server, database, paid service or new dependency. It works with the files the pipeline already publishes.

## What is published

`summary.json` (about 0.9 KB) gains a small manifest. Nothing else in it, and nothing in `data.json`, changes.

```json
"report": { "version": "LPJNul-wow4m6DsqxbninhsW", "data_file": "data.json", "data_bytes": 112403, "rows": 300 }
```

- `version` is the first 18 bytes of the SHA-256 of the exact bytes of `data.json`, written as 24 base64url characters
  (`pipeline.build.report_version`). It identifies the published report itself, not a moment in time: different data means a
  different version. It is base64url rather than hex because a hex digest can contain ten digits in a row, which the privacy
  check's phone-number pattern would flag by chance (about 8% of random digests; 0 of 50,000 for base64url).
- `data_bytes` and `rows` let the page check that what it downloaded is complete.
- A page published before this change has no `report`; the page then falls back to `refreshed_at` (see Limitations).

## What the page does

1. After the first load (unchanged: both files, same checks), it starts an update check.
2. Every **60 seconds** while the tab is visible it requests `summary.json` only (about 0.9 KB), at that plain address. The
   request revalidates with the server (`cache: 'no-cache'`): the browser sends the `ETag` it stored and a `304 Not Modified`
   costs headers only. There is deliberately no changing query key: a new address for every check would leave the browser
   nothing to validate, so every check would be a full response. This does not make a shared cache (the CDN) hand over a newer
   file than it holds; see Caching below.
3. If the version is the same, nothing else happens: no data download, no redraw, nothing announced.
4. If it is new, it downloads `data.json?v=<version>` with `cache: 'no-store'` (so the browser never stores a response
   before it is verified; the version in the address names what is wanted and shows in logs, but it does not get past a CDN
   cache) and checks it before anything on screen changes:
   - the SHA-256 of the downloaded bytes gives the manifest's version;
   - the size and the row count equal the manifest's, and `refreshed_at` equals the summary's;
   - there is at least one row, and every row has every field the page reads, with the expected types.
5. Only then are the new tables computed (a failure here leaves the old report untouched), swapped in and drawn. If drawing
   fails after the swap, the previous report is put back. The reader's filters (agent, weekly/monthly) are kept, and the agent list is
   rebuilt only if it changed, so an open dropdown is not disturbed.
6. If the selected agent is not in the new report, the view shows all agents and says so (next section).

### When the selected agent is not in a new report

The filters are stored apart from the report and are never changed by an update. If a newly accepted report does not contain
the selected agent, the page shows all agents (as it always did for an unknown agent) and now explains it:

- A note under the headline: "The selected agent, X, is not in the latest report, so this view shows all agents. The selection
  is kept and comes back if X appears in a later report. Choose another agent or press Reset to clear it."
- The stored selection is kept, so when a later report contains the agent again the view goes back to them by itself, the note
  disappears and "Showing X again." is spoken once.
- Choosing another agent, or Reset, clears the note without any announcement.
- The spoken message (a polite status region, separate from the update-check one) is said once when the situation starts. It is
  not repeated by redraws, resizes, filter changes or later reports that still lack the agent, and it is said again only if the
  agent returns and then goes missing again. The name is placed on the page as text, never as markup.
- "All agents" in the dropdown is already what is displayed, so choosing it does not fire a change; use Reset or another agent.

A response is never allowed to replace valid data when it is: not JSON, not an object, missing or unreadable `refreshed_at`,
a malformed manifest, an older report than the one shown (a stale cached copy), an oversized response, empty, incomplete,
inconsistent with its summary, or hash-mismatched.

## Request rate and back-off

| Situation | Behaviour |
|---|---|
| Healthy, tab visible | one small request every 60 seconds (about one a minute), each varied by up to plus or minus 10% (54 to 66 seconds) so open tabs do not call in step |
| Tab hidden | no requests; one check shortly after the tab becomes visible again |
| Browser comes back online | one check at once |
| Request fails, times out (10 seconds) or the summary is malformed | wait 2, 4 and 8 minutes, then 10 minutes at most (the interval doubles per failure, capped); reset by the next good check |
| New summary seen but its data does not match yet (CDN lag) | look again after 5, 10 and 20 seconds (the address gets `&r=n`, which only distinguishes the browser's own cache entries; the waiting is what helps); after the third, treat as a failure and back off |
| A check is already running | a second one is refused: never two requests at once |

Cost while healthy: about 60 requests an hour per visible tab. Measured on the live site (today's 790-byte summary), a full
response is about 1,525 bytes on the wire (735 of headers) and a conditional request answered 304 about 395, so an unchanged
report costs about 24 KB an hour per tab (about 92 KB if every check were a full response). The summary with its manifest is
about 100 bytes larger.
To see that updates cannot be checked takes three failed checks in a row: about 7 minutes of trouble, where the same rule at a
15 second interval took under two. One report download (about 112 KB) happens only when a new report is published.

## Caching: what was observed on the live site

Read-only requests to the published site, 25 Oct 2026. `Age` was sampled every 30 seconds for 12 minutes.

| Observation | Consequence |
|---|---|
| Every file is served with `Cache-Control: max-age=600` and an `ETag` | a default `fetch` may reuse a stored copy for 10 minutes, so the page forces revalidation |
| A conditional request (`If-None-Match`) is answered `304 Not Modified`: about 395 bytes, against about 1,525 for a full `summary.json` response | checks are cheap when the browser can validate. All 30 conditional requests sent over a minute (one per 2 seconds, answered by different edge servers) got `304` for the same `ETag` |
| The edge's `Age` for an unchanged file rises steadily (0 to 585 seconds in the samples, from different edge servers) and then restarts from 0 | the edge holds a copy for up to the full 600 seconds, so a changed file can keep being served with its old content for that long unless something refreshes it earlier |
| Different query strings (`?v=1`, an unused number, `?v=aaaa&r=1`) were answered from the same cached object: same `Age`, cache hits, and a conditional request with a never-used query was answered 304 | query strings do not get a request past the edge cache. Neither a per-minute key nor `data.json?v=<version>` / `&r=n` can force a fresh copy there; they only differ for the browser's own cache |
| A request header `Cache-Control: no-cache` made no visible difference at the edge (earlier observation, not repeated) | the page does not rely on request headers to reach the origin |

**Not verified.** Whether a new deployment purges or refreshes the edge's copies. No deployment was made, so how soon a new
`summary.json` and `data.json` are served after a publish is unknown. GitHub's Pages documentation says changes can take up to
10 minutes to be published after a push; if the edge simply ages its copies out, a new report could be served up to about 10
minutes after the deploy. The page notices it at the first check after that, so the total can be about 10 minutes plus one
polling interval. Revalidation does not shorten this and nothing here guarantees immediate invalidation.

The summary and the data are separate files, so for a while the edge may serve a new summary beside the old data file (or the
reverse). The page never displays that: the data must match the summary's hash, size, row count and timestamp, otherwise it
looks again after 5, 10 and 20 seconds, then backs off, and the report on screen is untouched.

## Accessibility

- The visible line "Checks for new reports automatically · last checked 4:02 PM" changes at every check, so it is **not** a live
  region and is never announced.
- A separate visually hidden region (`role="status"`, `aria-live="polite"`) speaks only when something matters: a newer
  report was loaded, updates cannot be checked (after three failed checks), updates are working again. Each is spoken once.
- The missing-agent note is plain text on the page (not a live region) with its own hidden polite region that speaks once when
  the situation starts and once when the agent returns; see "When the selected agent is not in a new report".
- Focus and scroll position are not moved. Messages are plain text; no colour-only signal (the warning state is also worded).
- No new animation.

## Limitations

- **Delay.** The page notices a new report at the first check after the CDN starts serving it: at most 66 seconds (one interval
  plus 10%), about 30 on average, later. How long the CDN takes after a deployment is not known (see Caching: edge copies can
  be held for up to 10 minutes, and invalidation on deploy was not verified), so the end-to-end delay can be about 10 minutes
  plus an interval. It was not measured because it needs a real deployment. The verification and the 5/10/20 second retries
  exist for a lagging or half-updated edge.
- **60 seconds is still more frequent than the data changes** (the recommended refresh cadence is 30 minutes). The interval is
  one constant (`intervalMs` in `site/index.html`); it was 15 seconds in the first version of this change and was lengthened to
  cut requests to a quarter at the cost of a slower pick-up.
- **Stale cached copies** of `summary.json` older than the report shown are ignored (the page never goes backwards). This also
  means a deliberate re-publication with an earlier `refreshed_at` would be ignored by open tabs; the pipeline always
  stamps the current time.
- **Pages published before this change** have no manifest. Open tabs loaded from them have no poller until reloaded once. A
  new page reading an old summary falls back to `refreshed_at`: it still validates the downloaded data's structure, size
  and timestamp, but cannot check a hash.
- **No hashing without a secure context** (`file://` or plain `http` other than localhost): the check then uses size, row
  count, timestamp and structure only.
- **The hash detects mismatched or corrupted files, not tampering** by whoever controls the site.
- **States.** The page reports "last checked" and "cannot check". It does not know a refresh is running (`refreshing`) or that
  the last publish failed (`failed`); that needs the recovery work planned for a later phase.
- **Background tabs** are not polled at all, so a long-hidden tab shows its old report until it becomes visible, then checks
  within a few seconds.
- A client with a wrong clock affects only the "last checked" time and the days-old notice; it cannot change which report is shown.

## Checked in a real browser (25 Oct 2026)

The first run used a 15 second interval and a per-minute key on the summary address. After the interval was changed to 60
seconds, and again after the key was removed and the missing-agent note added, scenarios were repeated in a real browser
(results below); the 500-error and background-tab rows were measured in the first run and are covered at 60 seconds by the
automated tests (the back-off formula and the visibility handling were not changed).

Three different reports were built with the real pipeline and served by a local stand-in for GitHub Pages (same headers as
measured: `max-age=600`, `ETag`, 304), with every request logged. The real page was opened in a browser and the files were
replaced underneath it. This is a local simulation, not the live site, and the CDN's behaviour right after a deploy could not
be reproduced.

| Scenario | Observed |
|---|---|
| Publish a new report while the page is open (60 s interval) | picked up at the next check, 2 s and 51 s after publishing in two runs, with no reload (same document, one navigation); `data.json?v=<version>` downloaded once per new report (113 KB); healthy checks were 61 s apart; the agent and Monthly selections were kept; the screen-reader region said "A newer report was loaded" |
| Plain `summary.json`, no query (60 s interval, after removing the key) | the first poll of a page sent its stored validator and the stand-in answered 200 when the report had changed, 304 when it had not (log: `GET /summary.json 200 ... conditional`, later `GET /summary.json 304 0 conditional`); a changed report was detected at the next check and `data.json?v=<version>` fetched once |
| Selected agent absent from the new report (60 s interval) | the note appeared with the new report, the view showed all agents (dropdown "All agents"), the Monthly choice was kept, the stored agent was kept, and the polite region received its message once (one text change recorded); no reload |
| The agent is back in a later report | the note disappeared, the dropdown showed the agent again, "Showing X again." was spoken once, Monthly still kept |
| Summary announces a new report but the data file is still the old one (60 s interval) | refused every time (hash/size mismatch); retried after about 5, 10 and 21 s with `&r=1..3`; the old report stayed on screen, no error banner and the selections were untouched; the next check came 121 s later (the first back-off step, 2 minutes) and accepted the report once the data file was correct |
| Server answering 500 (first run, 15 s interval, timers compressed 10x for the test) | waits doubled (3 s, 6 s, shown in real time as 30 s, 60 s); after the third failure one spoken notice and a worded warning line; report unchanged; on recovery "Updates are working again." spoken once and the warning cleared |
| Tab in the background (first run, 15 s interval) | 0 requests in 60 s; polling resumed when the tab was shown again |
| A redraw with an unchanged agent list | the dropdown's option elements are kept (an open dropdown is not disturbed); they are rebuilt only when the agent list changes |

## Tests

- `pipeline/test_dashboard_polling.py`: manifest correctness, version vectors shared with the browser code, privacy check,
  unchanged summary keys and data fields, documented numbers equal the code's.
- `tests_js/poll.test.mjs` (polling logic) and `tests_js/dash.test.mjs` (data layer): new versions, unchanged versions,
  caching modes and addresses, conditional requests and 304s through a model of a browser cache in front of a Pages-like
  server (including an edge that keeps serving the old files for a while), stale copies, network errors, HTTP errors, timeouts,
  malformed summaries and data, mismatch retries, back-off, trouble and recovery, hidden tabs, one request at a time, failed
  rendering, filters untouched by every kind of update, and the missing-agent note (once, cleared, never as markup).
  Run with Node's built-in runner, no packages: `node --test tests_js/poll.test.mjs tests_js/dash.test.mjs`
  (pass the files, not the folder). `pytest pipeline` runs them too when Node is installed.

## Rolling back

Revert the commit. The old page ignores the extra `report` key in `summary.json`, so no coordinated change is needed.
