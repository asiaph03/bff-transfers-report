"""Run-history guard, using only the standard library (the deploy job installs nothing).

    python3 -m pipeline.run_guard --repo OWNER/NAME --run-id N [--workflow refresh.yml] [--api-url URL]

Question it answers, before anything is deployed: has a NEWER run of this workflow already reached the deployment step? If so, this
(older) run must not publish over it, whatever the CDN currently shows. The CDN-based guard in live_check.py stays in place; this one
supplements it with evidence that does not depend on cached files.

Evidence comes from the Actions API: the workflow's runs (newest first) and, for every run with a higher id than this one, its jobs from
ALL attempts. A run counts as having published when its deploy job's deploy-pages STEP concluded `success`, even if a later step (the live
check) failed. A preview run has a skipped deploy job and never blocks. A newer run whose deployment may have happened but whose outcome is
a failure, cancellation, timeout or anything unclear fails closed for an older run, as does a newer run that is already executing (runs are
serialized by the concurrency group, so an executing newer run means that assumption failed). A newer run that is only queued or waiting
has not reached deployment, will deploy after this one if at all, and does not block.

`run_id` is the same across re-runs: re-running the newest run is allowed (nothing is newer), re-running an older run is refused once a
newer run has reached deployment. Any problem that could change the decision (permission or HTTP errors after bounded retries, malformed or
incomplete data, unexpected ordering, a missing deploy job, an unexpected step name, an unknown state) refuses. There is no first-release
exemption: a first release has no newer runs and the API simply says so.

The token is read from the environment (GH_TOKEN, the job's GITHUB_TOKEN with `actions: read`), only ever sent to the API host, never
follows a redirect, and is never printed. Messages name run ids, numbers and states only.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

API_URL = "https://api.github.com"
WORKFLOW_FILE = "refresh.yml"
WORKFLOW_PATH = ".github/workflows/refresh.yml"
# Pinned by pipeline/test_workflow.py against the workflow itself: change one only together with the other.
DEPLOY_JOB_NAME = "Publish to GitHub Pages"
DEPLOY_STEP_NAME = "Deploy to GitHub Pages"

PER_PAGE = 100
MAX_RUN_PAGES = 5            # at most 500 runs of history are read; more is "incomplete"
MAX_JOB_PAGES = 3
MAX_NEWER_RUNS = 100
REQUEST_TIMEOUT = 20.0       # seconds, per request
ATTEMPTS = 3                 # per request
BACKOFF = (2.0, 5.0)         # seconds before the 2nd and 3rd attempt (server errors, network failures)
MAX_WAIT = 60.0              # the longest single wait, including a Retry-After from GitHub; asking for more refuses
DEADLINE = 240.0             # seconds for the whole check
MAX_BODY = 5_000_000

RUN_NOT_STARTED = {"queued", "requested", "waiting", "pending"}
RUN_STATUSES = RUN_NOT_STARTED | {"in_progress", "completed"}
NEVER_STARTED = ("cancelled", "startup_failure")      # a completed run with no job at all and one of these conclusions never ran anything
JOB_NOT_STARTED = {"queued", "waiting", "pending", "requested"}
JOB_STATUSES = JOB_NOT_STARTED | {"in_progress", "completed"}
SEVERITY = {"deployed": 4, "in-flight": 3, "active": 2, "ambiguous": 1, "none": 0}


class GuardError(Exception):
    """The history could not be established safely. The message is safe to print."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):         # never follow a redirect: the token must only go to the API host
        return None


def http_opener(url: str, headers: dict, timeout: float):
    """(status, headers, body). Raises OSError-like errors for network failures; 3xx/4xx/5xx come back as statuses."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as r:
            body = r.read(MAX_BODY + 1)
            return r.status, dict(r.headers), body
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), b""


def _retry_after(headers: dict):
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _next_link(headers: dict):
    m = re.search(r'<([^>]+)>\s*;\s*rel="next"', headers.get("link", ""))
    return m.group(1) if m else None


class Api:
    def __init__(self, token: str, base: str = API_URL, opener: Callable = http_opener, sleep: Callable = time.sleep,
                 clock: Callable = time.monotonic, wall: Callable = time.time, deadline: float = DEADLINE):
        u = urllib.parse.urlparse(base)
        loopback = u.hostname in ("127.0.0.1", "localhost", "::1")
        if not (u.scheme == "https" or (u.scheme == "http" and loopback)) or not u.hostname:
            raise GuardError("the API address must be https (plain http only for a local test server)")
        self.token, self.base, self.opener, self.sleep, self.clock, self.wall = token, base.rstrip("/"), opener, sleep, clock, wall
        self.deadline, self.started = deadline, clock()
        self.requests = 0

    def redact(self, text: str) -> str:
        return text.replace(self.token, "***") if self.token else text

    def _url(self, path_or_url: str) -> str:
        if path_or_url.startswith("/"):
            return self.base + path_or_url
        if path_or_url.startswith(self.base + "/"):                # a pagination link must stay on the API host
            return path_or_url
        raise GuardError("a pagination link pointed outside the API; refusing to send the token there")

    def get(self, path_or_url: str):
        """(parsed JSON, lower-cased response headers) or GuardError. Bounded retries; honors Retry-After up to MAX_WAIT."""
        url = self._url(path_or_url)
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "bff-run-guard"}
        what = urllib.parse.urlparse(url).path.replace(self.base, "")
        last, wait = "", 0.0
        for attempt in range(1, ATTEMPTS + 1):
            if self.clock() - self.started > self.deadline:
                raise GuardError("the time budget for this check ran out")
            self.requests += 1
            try:
                status, hdrs, body = self.opener(url, headers, REQUEST_TIMEOUT)
            except (OSError, http.client.HTTPException) as e:        # URLError and timeouts are OSErrors; the message names only the type
                last, wait = f"network error ({type(e).__name__})", BACKOFF[min(attempt - 1, len(BACKOFF) - 1)]
            else:
                hdrs = {str(k).lower(): str(v) for k, v in hdrs.items()}
                if status == 200:
                    if len(body) > MAX_BODY:
                        raise GuardError(f"{what}: response too large")
                    try:
                        return json.loads(body.decode("utf-8")), hdrs
                    except (UnicodeDecodeError, ValueError):
                        raise GuardError(f"{what}: malformed JSON in the response") from None
                if status == 401:
                    raise GuardError("HTTP 401: the token was rejected")
                if status == 404:
                    raise GuardError(f"HTTP 404 for {what}: not found, or the token cannot see it")
                if status in (403, 429):
                    ra = _retry_after(hdrs)
                    reset = hdrs.get("x-ratelimit-reset", "")
                    limited = status == 429 or ra is not None or hdrs.get("x-ratelimit-remaining") == "0"
                    if not limited:
                        raise GuardError("HTTP 403: not allowed; the deploy job needs the permission `actions: read`")
                    if ra is None and reset.isdigit():
                        ra = max(0.0, float(reset) - self.wall())
                    wait = BACKOFF[min(attempt - 1, len(BACKOFF) - 1)] if ra is None else ra
                    if wait > MAX_WAIT:
                        raise GuardError(f"HTTP {status}: rate limited; GitHub asks for {wait:.0f} s, more than the {MAX_WAIT:.0f} s this check will wait")
                    last = f"HTTP {status} (rate limited)"
                elif 500 <= status <= 599:
                    last, wait = f"HTTP {status}", BACKOFF[min(attempt - 1, len(BACKOFF) - 1)]
                else:
                    raise GuardError(f"HTTP {status} for {what}")
            if attempt == ATTEMPTS:
                raise GuardError(f"{last} for {what} after {ATTEMPTS} attempts")
            if self.clock() - self.started + wait > self.deadline:
                raise GuardError("the time budget for this check ran out")
            self.sleep(wait)
        raise GuardError("unreachable")                                # pragma: no cover


def _is_id(x) -> bool:
    return type(x) is int and x > 0


def newer_runs(api: Api, repo: str, workflow: str, run_id: int) -> list:
    """Every run of the workflow with an id above `run_id`, from a verified, complete, newest-first history."""
    url = f"/repos/{repo}/actions/workflows/{workflow}/runs?per_page={PER_PAGE}"
    newer, pages, previous_last, found, reached = [], 0, None, False, False
    while True:
        pages += 1
        data, headers = api.get(url)
        runs = data.get("workflow_runs") if isinstance(data, dict) else None
        if not isinstance(runs, list) or type(data.get("total_count")) is not int:
            raise GuardError("run history: malformed response (no run list or count)")
        ids = []
        for r in runs:
            if not isinstance(r, dict) or not _is_id(r.get("id")) or not isinstance(r.get("status"), str):
                raise GuardError("run history: a run record is malformed")
            if r.get("path") != WORKFLOW_PATH:
                raise GuardError("run history: a run does not belong to the expected workflow file")
            ids.append(r["id"])
        if any(a <= b for a, b in zip(ids, ids[1:])) or (previous_last is not None and ids and ids[0] >= previous_last):
            raise GuardError("run history: the runs are not in the expected newest-first order")
        for r in runs:
            if r["id"] == run_id:
                found = True
            elif r["id"] > run_id:
                newer.append(r)
        if ids:
            previous_last = ids[-1]
        reached = reached or any(i <= run_id for i in ids)
        nxt = _next_link(headers)
        if reached or not nxt or not runs:
            break
        if pages >= MAX_RUN_PAGES:
            raise GuardError(f"run history is longer than the {MAX_RUN_PAGES * PER_PAGE} runs this check reads; it is incomplete")
        url = nxt
    if not found:
        raise GuardError(f"this run ({run_id}) is not in the workflow's run history, so what is newer cannot be established")
    if len(newer) > MAX_NEWER_RUNS:
        raise GuardError(f"more than {MAX_NEWER_RUNS} newer runs exist; dispatch a new run instead")
    return newer


def jobs_of(api: Api, repo: str, run_id: int) -> list:
    """All jobs of all attempts of a run, or GuardError if the list is malformed or incomplete."""
    url = f"/repos/{repo}/actions/runs/{run_id}/jobs?filter=all&per_page={PER_PAGE}"
    jobs, pages, total = [], 0, None
    while url:
        pages += 1
        data, headers = api.get(url)
        part = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(part, list) or type(data.get("total_count")) is not int:
            raise GuardError(f"run {run_id}: malformed job list")
        total = data["total_count"]
        jobs += part
        url = _next_link(headers)
        if url and pages >= MAX_JOB_PAGES:
            raise GuardError(f"run {run_id}: the job list is longer than this check reads; it is incomplete")
    if len(jobs) != total:
        raise GuardError(f"run {run_id}: the job list is incomplete ({len(jobs)} of {total} jobs)")
    for j in jobs:
        if not isinstance(j, dict) or not isinstance(j.get("name"), str) or not isinstance(j.get("status"), str):
            raise GuardError(f"run {run_id}: a job record is malformed")
    return jobs


def _step_state(step: dict) -> str:
    status, conclusion = step.get("status"), step.get("conclusion")
    if conclusion == "success":
        return "deployed"
    if conclusion == "skipped":
        return "none"
    if status in ("pending", "queued") and conclusion is None:
        return "none"
    if status == "in_progress":
        return "in-flight"
    return "ambiguous"                      # failure, cancelled, timed out, completed without a conclusion, anything unknown


def _job_state(job: dict, run_id: int) -> str:
    status, conclusion = job["status"], job.get("conclusion")
    if status not in JOB_STATUSES:
        raise GuardError(f"run {run_id}: the deploy job is in an unknown state")
    steps = job.get("steps")
    steps = [] if steps is None else steps
    if not isinstance(steps, list):
        raise GuardError(f"run {run_id}: the deploy job's steps are malformed")
    named = [s for s in steps if isinstance(s, dict) and s.get("name") == DEPLOY_STEP_NAME]
    if len(named) > 1:
        raise GuardError(f"run {run_id}: the deploy job has more than one step named '{DEPLOY_STEP_NAME}'")
    if named:
        return _step_state(named[0])
    if steps:
        raise GuardError(f"run {run_id}: the deploy job has no step named '{DEPLOY_STEP_NAME}' (renamed?), so a deployment cannot be ruled out")
    # no step records at all. A job that began running lists every one of its steps (later ones as pending), so no records means no step
    # began: a skipped job (a preview) or a job cancelled before a runner took it never ran the deploy step, whether or not it has a start time.
    if status == "completed":
        if conclusion in ("skipped", "cancelled"):
            return "none"
        if conclusion == "success":
            return "deployed"               # a successful job cannot have skipped its deploy step
        return "ambiguous" if job.get("started_at") else "none"   # failed or timed out after starting, recording nothing: unclear
    if status in JOB_NOT_STARTED:
        return "none"
    return "in-flight"                      # in progress with no step list yet


def classify_run(run: dict, jobs: list) -> tuple:
    """(verdict, detail) for one newer run: deployed | in-flight | active | ambiguous | none."""
    rid, status = run["id"], run["status"]
    if status not in RUN_STATUSES:
        raise GuardError(f"run {rid}: unknown run state")
    deploy_jobs = [j for j in jobs if j.get("name") == DEPLOY_JOB_NAME]
    if not deploy_jobs and status == "completed":
        if not jobs and run.get("conclusion") in NEVER_STARTED:
            return "none", ""               # cancelled (for instance a pending run replaced by a newer dispatch) before any job of any attempt existed
        raise GuardError(f"run {rid}: no job named '{DEPLOY_JOB_NAME}', so a preview cannot be told from a publication")
    verdicts = [_job_state(j, rid) for j in deploy_jobs]
    if status == "in_progress":
        verdicts.append("active")           # serialized runs never overlap: an executing newer run is not expected
    verdict = max(verdicts, key=lambda v: SEVERITY[v], default="none")
    if verdict == "ambiguous":
        step_conclusions = sorted({re.sub(r"[^a-z_]", "", str(s.get("conclusion") or s.get("status") or ""))
                                   for j in deploy_jobs for s in (j.get("steps") or []) if isinstance(s, dict) and s.get("name") == DEPLOY_STEP_NAME})
        if any(c for c in step_conclusions):
            return verdict, "its deployment step ended " + "/".join(c for c in step_conclusions if c)
        ended = sorted({re.sub(r"[^a-z_]", "", str(j.get("conclusion") or j.get("status") or "")) for j in deploy_jobs} - {""})
        return verdict, "its deploy job ended " + ("/".join(ended) or "unclear") + " without recording any step"
    return verdict, ""


@dataclass
class Decision:
    ok: bool
    message: str


def check(api: Api, repo: str, run_id: int, workflow: str = WORKFLOW_FILE) -> Decision:
    newer = newer_runs(api, repo, workflow, run_id)
    blockers, summary = [], {"none": 0}
    for r in sorted(newer, key=lambda x: x["id"], reverse=True):
        verdict, detail = classify_run(r, jobs_of(api, repo, r["id"]))
        summary[verdict] = summary.get(verdict, 0) + 1
        if verdict != "none":
            blockers.append((SEVERITY[verdict], r, verdict, detail))
    if blockers:
        _, r, verdict, detail = max(blockers, key=lambda b: (b[0], b[1]["id"]))
        who = f"run {r['id']}" + (f" (#{r['run_number']})" if _is_id(r.get("run_number")) else "")
        reason = {
            "deployed": f"{who} has already deployed to GitHub Pages and is newer than this run ({run_id})",
            "in-flight": f"{who} is deploying to GitHub Pages right now and is newer than this run ({run_id})",
            "active": f"{who} is executing now and is newer than this run ({run_id}); runs are meant to be serialized",
            "ambiguous": f"{who} reached the deployment step and is newer than this run ({run_id}), but its outcome is unclear ({detail}); it may have gone live",
        }[verdict]
        return Decision(False, f"REFUSED: {reason}. An older run never publishes over a newer one. Dispatch a new run instead (a new run has the highest run id).")
    return Decision(True, f"ALLOWED: none of the {len(newer)} newer run(s) of this workflow has reached deployment "
                          f"(previews, failed runs or runs still queued). {api.requests} API request(s).")


def main(argv=None, opener: Callable = http_opener, sleep: Callable = time.sleep) -> int:
    ap = argparse.ArgumentParser(description="Refuse to publish over a newer run (Actions API, standard library only).")
    ap.add_argument("--repo", required=True, help="OWNER/NAME")
    ap.add_argument("--run-id", required=True, type=int)
    ap.add_argument("--workflow", default=WORKFLOW_FILE)
    ap.add_argument("--api-url", default=API_URL, help="for tests with a local server; the default is the GitHub API")
    a = ap.parse_args(argv)
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        print("REFUSED: GH_TOKEN is not set, so the run history cannot be read.")
        return 1
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", a.repo) or a.run_id <= 0 or not re.fullmatch(r"[A-Za-z0-9_.-]+\.ya?ml", a.workflow):
        print("REFUSED: the repository, run id or workflow name is not valid.")
        return 1
    api = None
    try:
        api = Api(token, a.api_url, opener=opener, sleep=sleep)
        d = check(api, a.repo, a.run_id, a.workflow)
        print(api.redact(d.message))
        return 0 if d.ok else 1
    except GuardError as e:
        print(f"REFUSED: the publication history could not be established safely: {(api.redact(str(e)) if api else str(e).replace(token, '***'))}")
        return 1
    except Exception as e:                  # noqa: BLE001 - fail closed on anything unexpected, naming only its type
        print(f"REFUSED: the publication history could not be established ({type(e).__name__}).")
        return 1


if __name__ == "__main__":
    sys.exit(main())
