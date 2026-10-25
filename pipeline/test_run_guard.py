"""The run-history guard (pipeline/run_guard.py): an older run must never publish over a newer run that reached deployment.

Everything here uses a fake API (scripted pages, jobs, errors) or a local HTTP stand-in; nothing talks to GitHub and nothing is deployed.
Run ids are made up. Evidence modelled on the real API: runs newest first with `path`, jobs of all attempts with `name`, `status`,
`conclusion`, `run_attempt` and `steps` (a skipped deploy job has no steps).
"""
from __future__ import annotations

import functools
import http.server
import inspect
import json
import threading
import urllib.parse

import pytest

from pipeline import run_guard
from pipeline.run_guard import Api, GuardError

BASE = "https://api.github.com"
REPO = "owner/repo"
TOKEN = "ghs_TOPSECRET0123456789"
JOB, STEP, PATH = run_guard.DEPLOY_JOB_NAME, run_guard.DEPLOY_STEP_NAME, run_guard.WORKFLOW_PATH

OK, SKIP, FAIL = ("completed", "success"), ("completed", "skipped"), ("completed", "failure")
CANCEL, TIMEOUT = ("completed", "cancelled"), ("completed", "timed_out")
RUNNING, PENDING = ("in_progress", None), ("pending", None)
ME = 200                                   # the id of the run asking for permission to publish


# ---------- builders for runs, jobs and steps ----------

def run(rid, status="completed", conclusion="success"):
    return {"id": rid, "run_number": rid - 100, "status": status, "conclusion": conclusion, "path": PATH, "run_attempt": 1}


def step(name, state=OK):
    return {"name": name, "status": state[0], "conclusion": state[1], "number": 1}


def deploy_job(deploy=OK, live=OK, *, attempt=1, steps=None, status=None, conclusion=None, started=True):
    if steps is None:
        steps = [step("Set up job"), step("Run actions/checkout@v4"), step("Refuse to publish over a newer run"), step("Refuse to replace a newer live report"),
                 step(STEP, deploy), step("Verify the live site serves the candidate report", live), step("Complete job")]
    states = [(s["status"], s["conclusion"]) for s in steps]
    if status is None:
        status = "in_progress" if any(s[0] in ("in_progress", "pending", "queued") for s in states) else "completed"
    if conclusion is None and status == "completed":
        conclusion = "success" if all(s[1] in ("success", "skipped") for s in states) else "failure"
    return {"name": JOB, "status": status, "conclusion": conclusion, "run_attempt": attempt, "started_at": "2026-10-25T10:00:00Z" if started else None, "steps": steps}


def skipped_deploy_job(attempt=1):
    return {"name": JOB, "status": "completed", "conclusion": "skipped", "run_attempt": attempt, "started_at": "2026-10-25T10:00:00Z", "steps": []}


def build_job(state=OK, attempt=1):
    return {"name": "Test, fetch, calculate and validate", "status": state[0], "conclusion": state[1], "run_attempt": attempt, "steps": [step("Set up job")]}


def published(rid, **kw):
    return run(rid), [build_job(), deploy_job(**kw)]


def preview(rid):
    return run(rid), [build_job(), skipped_deploy_job()]


def build_failed(rid):
    return run(rid, conclusion="failure"), [build_job(FAIL), skipped_deploy_job()]


def queued(rid, with_jobs=True, status="queued"):
    jobs = [build_job(("queued", None)), deploy_job(steps=[], status="queued", conclusion=None, started=False)] if with_jobs else []
    return run(rid, status=status, conclusion=None), jobs


def building(rid):
    return run(rid, status="in_progress", conclusion=None), [build_job(("in_progress", None)), deploy_job(steps=[], status="queued", conclusion=None, started=False)]


# ---------- the fake API ----------

class FakeApi:
    """Callable like http_opener(url, headers, timeout). Serves paged runs and jobs; scripted responses (or exceptions) go first."""

    def __init__(self, history, base=BASE, runs_per_page=100, jobs_per_page=100):
        self.base, self.runs_per_page, self.jobs_per_page = base, runs_per_page, jobs_per_page
        self.runs = [r for r, _ in history]
        self.jobs = {r["id"]: j for r, j in history}
        self.script = []                    # (substring of url, [responses]) consumed in order
        self.requests = []                  # (url, headers, timeout)
        self.raw_runs = None                # optional callable(page) -> raw response dict, to serve malformed data

    def script_for(self, needle, *responses):
        self.script.append((needle, list(responses)))
        return self

    def _page(self, items, per_page, page, path_query, key, extra):
        chunk = items[(page - 1) * per_page: page * per_page]
        headers = {}
        if page * per_page < len(items):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(path_query).query))
            q["page"] = str(page + 1)
            headers["Link"] = f'<{self.base}{path_query.split("?")[0]}?{urllib.parse.urlencode(q)}>; rel="next"'
        return 200, headers, json.dumps({"total_count": len(items), key: chunk, **extra}).encode()

    def __call__(self, url, headers, timeout):
        self.requests.append((url, dict(headers), timeout))
        for needle, queue in self.script:
            if needle in url and queue:
                item = queue.pop(0)
                if isinstance(item, BaseException):
                    raise item
                return item
        assert url.startswith(self.base), url
        path_query = url[len(self.base):]
        parsed = urllib.parse.urlparse(path_query)
        q = dict(urllib.parse.parse_qsl(parsed.query))
        page = int(q.get("page", "1"))
        m_runs = parsed.path == f"/repos/{REPO}/actions/workflows/refresh.yml/runs"
        m_jobs = parsed.path.startswith(f"/repos/{REPO}/actions/runs/") and parsed.path.endswith("/jobs")
        if m_runs:
            if self.raw_runs:
                return 200, {}, json.dumps(self.raw_runs(page)).encode()
            return self._page(self.runs, self.runs_per_page, page, path_query, "workflow_runs", {})
        if m_jobs:
            rid = int(parsed.path.split("/")[6])
            if rid not in self.jobs:
                return 404, {}, b""
            assert q.get("filter") == "all", "jobs must be requested for all attempts"
            return self._page(self.jobs[rid], self.jobs_per_page, page, path_query, "jobs", {})
        return 404, {}, b""

    def hosts(self):
        return {urllib.parse.urlparse(u).netloc for u, _, _ in self.requests}


class Clock:
    def __init__(self):
        self.t = 0.0
        self.waits = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.waits.append(s)
        self.t += s


def decide(history_or_api, run_id=ME, *, runs_per_page=100, jobs_per_page=100, deadline=run_guard.DEADLINE, wall=lambda: 1_700_000_000.0):
    api_fake = history_or_api if isinstance(history_or_api, FakeApi) else FakeApi(history_or_api, runs_per_page=runs_per_page, jobs_per_page=jobs_per_page)
    clock = Clock()
    api = Api(TOKEN, BASE, opener=api_fake, sleep=clock.sleep, clock=clock.now, wall=wall, deadline=deadline)
    try:
        return run_guard.check(api, REPO, run_id), api_fake, clock
    except GuardError as e:
        return run_guard.Decision(False, "ERROR: " + str(e)), api_fake, clock


def me(**kw):
    """The asking run, listed in the history like any other (it is in progress, building or deploying)."""
    return run(ME, status="in_progress", conclusion=None), [build_job(), deploy_job(steps=[], status="queued", conclusion=None, started=False)]


# ---------- the decisions ----------

def test_a_newer_successful_deployment_blocks_an_older_run():
    d, fake, _ = decide([published(300), me()])
    assert not d.ok and d.message.startswith("REFUSED: run 300 (#200) has already deployed to GitHub Pages")
    assert "Dispatch a new run instead" in d.message


def test_a_newer_preview_does_not_block():
    d, fake, _ = decide([preview(300), me()])
    assert d.ok and d.message.startswith("ALLOWED")


def test_a_newer_run_that_failed_before_deployment_does_not_block():
    d, _, _ = decide([build_failed(300), preview(250), me()])
    assert d.ok


@pytest.mark.parametrize("name,make", [
    ("queued with jobs", lambda: queued(300)),
    ("queued without jobs yet", lambda: queued(300, with_jobs=False)),
    ("waiting", lambda: queued(300, status="waiting")),
    ("pending", lambda: queued(300, status="pending")),
    ("requested", lambda: queued(300, status="requested", with_jobs=False)),
])
def test_a_newer_run_still_queued_has_not_reached_deployment_and_does_not_block(name, make):
    """It waits behind this run (the concurrency group); if it ever deploys it is newer, so this run publishing first does no harm."""
    d, _, _ = decide([make(), me()])
    assert d.ok, d.message


def test_a_newer_run_that_is_executing_blocks_because_runs_are_meant_to_be_serialized():
    d, _, _ = decide([building(300), me()])
    assert not d.ok and "is executing now" in d.message and "serialized" in d.message


def test_a_deployment_step_that_succeeded_counts_even_if_the_live_check_after_it_failed():
    d, _, _ = decide([(run(300, conclusion="failure"), [build_job(), deploy_job(deploy=OK, live=FAIL)]), me()])
    assert not d.ok and "has already deployed" in d.message


@pytest.mark.parametrize("state,word", [(FAIL, "failure"), (CANCEL, "cancelled"), (TIMEOUT, "timed_out")])
def test_a_deployment_step_that_failed_was_cancelled_or_timed_out_fails_closed(state, word):
    d, _, _ = decide([(run(300, conclusion="failure"), [build_job(), deploy_job(deploy=state, live=SKIP)]), me()])
    assert not d.ok and "outcome is unclear" in d.message and word in d.message and "may have gone live" in d.message


def test_a_deployment_step_still_running_blocks():
    d, _, _ = decide([(run(300, status="in_progress", conclusion=None), [build_job(), deploy_job(deploy=RUNNING, live=PENDING)]), me()])
    assert not d.ok and "deploying to GitHub Pages right now" in d.message


def test_a_successful_deploy_job_without_step_records_counts_as_deployed():
    d, _, _ = decide([(run(300), [build_job(), deploy_job(steps=[], status="completed", conclusion="success")]), me()])
    assert not d.ok and "has already deployed" in d.message


def test_a_deploy_job_cancelled_before_it_reached_the_deployment_step_does_not_block():
    steps = [step("Set up job"), step("Run actions/checkout@v4"), step("Refuse to publish over a newer run", CANCEL), step(STEP, SKIP), step("Verify the live site serves the candidate report", SKIP)]
    d, _, _ = decide([(run(300, conclusion="cancelled"), [build_job(), deploy_job(steps=steps, conclusion="cancelled")]), me()])
    assert d.ok, d.message


def test_a_guard_that_refused_leaves_a_deploy_step_that_was_skipped_so_it_does_not_block():
    steps = [step("Set up job"), step("Refuse to publish over a newer run", FAIL), step(STEP, SKIP), step("Verify the live site serves the candidate report", SKIP)]
    d, _, _ = decide([(run(300, conclusion="failure"), [build_job(), deploy_job(steps=steps, conclusion="failure")]), me()])
    assert d.ok, d.message


def test_a_deploy_job_that_started_but_recorded_no_steps_is_unclear_and_blocks():
    d, _, _ = decide([(run(300, conclusion="failure"), [build_job(), deploy_job(steps=[], status="completed", conclusion="failure", started=True)]), me()])
    assert not d.ok and "outcome is unclear" in d.message
    d, _, _ = decide([(run(300, conclusion="failure"), [build_job(), deploy_job(steps=[], status="completed", conclusion="failure", started=False)]), me()])
    assert d.ok


# ---------- runs replaced or cancelled before they started (the double-dispatch case) ----------
# Shapes seen in the public API: a cancelled run can have no jobs at all (total_count 0), and a cancelled job can have a start time and no
# step records, because a job that began running lists every step (later ones as pending).

CANCELLED_NO_JOBS = lambda rid=300, conclusion="cancelled": (run(rid, status="completed", conclusion=conclusion), [])
CANCELLED_JOB_NO_STEPS = lambda started=True: dict(deploy_job(steps=[], status="completed", conclusion="cancelled", started=started))


@pytest.mark.parametrize("conclusion", ["cancelled", "startup_failure"])
def test_a_newer_run_that_never_had_a_job_does_not_refuse_the_running_older_run(conclusion):
    d, fake, _ = decide([CANCELLED_NO_JOBS(300, conclusion), me()])
    assert d.ok, d.message


def test_the_double_dispatch_history_is_allowed_a_running_b_replaced_c_queued():
    """A is running, B was dispatched and queued, C was dispatched and replaced B (B cancelled, no jobs), C waits behind A."""
    d, fake, _ = decide([queued(302, with_jobs=False), CANCELLED_NO_JOBS(301), me(), published(150)])
    assert d.ok, d.message
    assert any("/runs/301/jobs" in u for u, _, _ in fake.requests), "the cancelled run's jobs were read, not assumed"


@pytest.mark.parametrize("conclusion", ["success", "failure", "timed_out", "neutral", "action_required", None])
def test_a_completed_run_with_no_jobs_and_any_other_conclusion_still_refuses(conclusion):
    d, _, _ = decide([(run(300, status="completed", conclusion=conclusion), []), me()])
    assert not d.ok and "no job named 'Publish to GitHub Pages'" in d.message


def test_a_cancelled_run_that_has_other_jobs_but_no_deploy_job_still_refuses():
    """Only a run with no job at all is 'never started'; otherwise a renamed deploy job could be hiding a deployment."""
    d, _, _ = decide([(run(300, status="completed", conclusion="cancelled"), [build_job(("completed", "cancelled"))]), me()])
    assert not d.ok and "no job named" in d.message


@pytest.mark.parametrize("started", [True, False])
def test_a_cancelled_deploy_job_that_recorded_no_step_does_not_block_with_or_without_a_start_time(started):
    d, _, _ = decide([(run(300, status="completed", conclusion="cancelled"), [build_job(("completed", "cancelled")), CANCELLED_JOB_NO_STEPS(started)]), me()])
    assert d.ok, d.message


@pytest.mark.parametrize("conclusion", ["failure", "timed_out"])
def test_a_started_deploy_job_that_failed_or_timed_out_recording_no_step_still_blocks_with_an_honest_message(conclusion):
    job = deploy_job(steps=[], status="completed", conclusion=conclusion, started=True)
    d, _, _ = decide([(run(300, conclusion="failure"), [build_job(), job]), me()])
    assert not d.ok and "outcome is unclear" in d.message
    assert f"its deploy job ended {conclusion} without recording any step" in d.message
    assert "deployment step ended unclear" not in d.message


def test_a_run_that_deployed_in_an_earlier_attempt_still_blocks_even_if_a_later_attempt_was_cancelled_with_no_jobs():
    jobs = [build_job(), deploy_job(attempt=1)]                       # attempt 1 deployed; attempt 2 left no job of its own
    d, _, _ = decide([(run(300, status="completed", conclusion="cancelled"), jobs), me()])
    assert not d.ok and "has already deployed" in d.message


def test_a_cancelled_deploy_job_that_did_record_its_deploy_step_as_cancelled_still_blocks():
    """A step record means the job had begun running; a deploy step cancelled while it ran may have gone live."""
    steps = [step("Set up job"), step(STEP, CANCEL), step("Verify the live site serves the candidate report", SKIP)]
    d, _, _ = decide([(run(300, status="completed", conclusion="cancelled"), [build_job(), deploy_job(steps=steps, conclusion="cancelled")]), me()])
    assert not d.ok and "outcome is unclear" in d.message and "cancelled" in d.message


def test_the_newest_run_can_proceed_when_nothing_is_newer():
    d, fake, _ = decide([me(), published(150)])
    assert d.ok and "0 newer run(s)" in d.message
    assert len(fake.requests) == 1, "only the run history is read; no job list is needed"


def test_re_running_the_newest_run_is_allowed_even_though_it_deployed_before():
    """The same run id keeps its own earlier (successful) deployment attempt; nothing is newer, so it may deploy the same report again."""
    history = [published(ME), published(150)]
    d, fake, _ = decide(history)
    assert d.ok
    assert not any(f"/runs/{ME}/jobs" in u for u, _, _ in fake.requests), "a run's own attempts are never examined"


def test_re_running_an_older_run_is_refused_once_a_newer_run_has_reached_deployment_whatever_the_cdn_shows():
    history = [published(300), published(ME), published(150)]
    d, fake, _ = decide(history)
    assert not d.ok and "run 300" in d.message
    assert fake.hosts() == {"api.github.com"}, "the decision uses the API only, never the cached site"


def test_older_runs_are_never_examined():
    d, fake, _ = decide([me(), published(190), published(150)])
    assert d.ok and not any("/runs/190/" in u or "/runs/150/" in u for u, _, _ in fake.requests)


def test_all_attempts_of_a_newer_run_are_read():
    for jobs in ([build_job(), deploy_job(attempt=1), deploy_job(steps=[], status="completed", conclusion="skipped", attempt=2)],
                 [build_job(), deploy_job(deploy=FAIL, live=SKIP, attempt=1), deploy_job(attempt=2)],
                 [build_job(), skipped_deploy_job(1), deploy_job(attempt=2)]):
        d, fake, _ = decide([(run(300), jobs), me()])
        assert not d.ok and "run 300" in d.message
        assert any("filter=all" in u for u, _, _ in fake.requests)
    d, _, _ = decide([(run(300), [build_job(), skipped_deploy_job(1), skipped_deploy_job(2)]), me()])
    assert d.ok


def test_the_strongest_evidence_among_several_newer_runs_is_reported():
    d, _, _ = decide([published(400), (run(350, conclusion="failure"), [build_job(), deploy_job(deploy=FAIL, live=SKIP)]), preview(300), me()])
    assert not d.ok and "run 400" in d.message and "already deployed" in d.message


# ---------- fail closed on history that cannot be trusted ----------

def test_a_missing_or_renamed_deploy_job_refuses():
    renamed = dict(deploy_job(), name="Deploy")
    for jobs in ([build_job()], [build_job(), renamed]):
        d, _, _ = decide([(run(300), jobs), me()])
        assert not d.ok and "no job named 'Publish to GitHub Pages'" in d.message


def test_a_renamed_or_duplicated_deploy_step_refuses():
    steps = [step("Set up job"), step("Run actions/deploy-pages@v4"), step("Complete job")]       # the old auto-generated name
    d, _, _ = decide([(run(300), [build_job(), deploy_job(steps=steps)]), me()])
    assert not d.ok and "no step named 'Deploy to GitHub Pages'" in d.message
    steps = [step(STEP), step(STEP)]
    d, _, _ = decide([(run(300), [build_job(), deploy_job(steps=steps)]), me()])
    assert not d.ok and "more than one step" in d.message


@pytest.mark.parametrize("name,history", [
    ("unknown run state", lambda: [(run(300, status="exploding"), [build_job(), skipped_deploy_job()]), me()]),
    ("unknown job state", lambda: [(run(300), [build_job(), dict(skipped_deploy_job(), status="weird")]), me()]),
    ("steps not a list", lambda: [(run(300), [build_job(), dict(skipped_deploy_job(), steps={"a": 1})]), me()]),
    ("job without a name", lambda: [(run(300), [build_job(), {"status": "completed"}]), me()]),
])
def test_unknown_states_and_malformed_records_refuse(name, history):
    d, _, _ = decide(history())
    assert not d.ok and d.message.startswith("ERROR:"), d.message


def test_an_unknown_state_of_the_deploy_step_itself_is_treated_as_possibly_deployed():
    weird = [step(STEP, ("completed", "mystery")), step("Verify the live site serves the candidate report", SKIP)]
    d, _, _ = decide([(run(300), [build_job(), deploy_job(steps=weird, conclusion="failure")]), me()])
    assert not d.ok and "outcome is unclear" in d.message


def test_run_history_paging_finds_newer_runs_on_later_pages_and_stops_at_the_current_run():
    history = [preview(305), preview(304), published(303), preview(302), preview(301), me(), published(150), published(140)]
    d, fake, _ = decide(history, runs_per_page=2)
    assert not d.ok and "run 303" in d.message
    pages = [u for u, _, _ in fake.requests if "/workflows/" in u]
    assert len(pages) == 3, "stops at the page that contains the current run"
    d, _, _ = decide([preview(305), preview(304), preview(303), me(), published(150)], runs_per_page=2)
    assert d.ok


def test_run_history_longer_than_the_bound_without_reaching_this_run_is_incomplete(monkeypatch):
    monkeypatch.setattr(run_guard, "MAX_RUN_PAGES", 2)
    history = [preview(r) for r in range(310, 300, -1)] + [me()]
    d, _, _ = decide(history, runs_per_page=3)
    assert not d.ok and "incomplete" in d.message


def test_too_many_newer_runs_refuses(monkeypatch):
    monkeypatch.setattr(run_guard, "MAX_NEWER_RUNS", 3)
    d, _, _ = decide([preview(r) for r in range(310, 304, -1)] + [me()])
    assert not d.ok and "more than 3 newer runs" in d.message


def test_unsorted_or_duplicated_run_history_refuses():
    for runs in ([run(300), run(310), me()[0]],                                  # not newest first within a page
                 [run(300), run(300), me()[0]],                                  # duplicate
                 ):
        fake = FakeApi([(r, []) for r in runs])
        d, _, _ = decide(fake)
        assert not d.ok and "newest-first order" in d.message
    fake = FakeApi([(r, [build_job(), skipped_deploy_job()]) for r in (run(310), run(305), run(307), me()[0])], runs_per_page=2)      # order breaks across pages
    d, _, _ = decide(fake)
    assert not d.ok and "newest-first order" in d.message


def test_a_history_that_does_not_contain_this_run_cannot_be_trusted():
    d, _, _ = decide([preview(300), published(150)])
    assert not d.ok and "not in the workflow's run history" in d.message


def _with_me(bad_run):
    """A history page whose first record is the given (bad) run and whose second is the asking run, so only the bad record can be the problem."""
    return {"total_count": 2, "workflow_runs": [bad_run, run(ME, status="in_progress", conclusion=None)]}


@pytest.mark.parametrize("name,raw,needle", [
    ("no run list", {"total_count": 1}, "malformed response"),
    ("no count", {"workflow_runs": []}, "malformed response"),
    ("count as text", {"total_count": "1", "workflow_runs": []}, "malformed response"),
    ("not an object", [1, 2], "malformed response"),
    ("run without id", _with_me({"status": "completed", "path": PATH}), "record is malformed"),
    ("id as text", _with_me({"id": "300", "status": "completed", "path": PATH}), "record is malformed"),
    ("id as boolean", _with_me({"id": True, "status": "completed", "path": PATH}), "record is malformed"),
    ("run without status", _with_me({"id": 300, "path": PATH}), "record is malformed"),
    ("another workflow's run", _with_me({"id": 300, "status": "completed", "path": ".github/workflows/other.yml"}), "does not belong to the expected workflow"),
    ("run without a path", _with_me({"id": 300, "status": "completed"}), "does not belong to the expected workflow"),
])
def test_malformed_run_history_refuses(name, raw, needle):
    fake = FakeApi([me()])
    fake.raw_runs = lambda page: raw
    d, _, _ = decide(fake)
    assert not d.ok and d.message.startswith("ERROR:") and needle in d.message, d.message


def test_job_list_paging_is_followed_and_an_incomplete_list_refuses(monkeypatch):
    jobs = [build_job(), deploy_job(), build_job(attempt=2), deploy_job(attempt=2)]
    d, fake, _ = decide(FakeApi([(run(300), jobs), me()], jobs_per_page=1))
    assert not d.ok and "run 300" in d.message
    assert len([u for u, _, _ in fake.requests if "/jobs" in u]) >= 2, "the deploy job on a later page was read"
    # the list says 4 jobs but only 2 are served and there is no next page
    fake = FakeApi([(run(300), [build_job(), skipped_deploy_job()]), me()])
    original = fake._page

    def lying(items, per_page, page, path_query, key, extra):
        s, h, b = original(items, per_page, page, path_query, key, extra)
        body = json.loads(b)
        if key == "jobs":
            body["total_count"] = 4
        return s, h, json.dumps(body).encode()
    fake._page = lying
    d, _, _ = decide(fake)
    assert not d.ok and "job list is incomplete (2 of 4 jobs)" in d.message
    monkeypatch.setattr(run_guard, "MAX_JOB_PAGES", 1)
    d, _, _ = decide(FakeApi([(run(300), jobs), me()], jobs_per_page=1))
    assert not d.ok and "incomplete" in d.message


def test_malformed_job_lists_refuse():
    fake = FakeApi([(run(300), []), me()])
    fake.jobs[300] = "not a list"
    d, _, _ = decide(fake)
    assert not d.ok and d.message.startswith("ERROR:")
    fake = FakeApi([preview(300), me()])
    fake.script_for("/runs/300/jobs", (200, {}, b'{"jobs": [], "total_count": "0"}'))
    d, _, _ = decide(fake)
    assert not d.ok and "malformed job list" in d.message


# ---------- API errors, bounded retries, Retry-After ----------

def ok_history():
    return [published(300), me()]


def refused(fake, **kw):
    d, f, c = decide(fake, **kw)
    assert not d.ok, d.message
    return d, f, c


@pytest.mark.parametrize("status,needle", [(401, "token was rejected"), (403, "needs the permission `actions: read`"), (404, "HTTP 404"), (418, "HTTP 418"), (302, "HTTP 302")])
def test_authentication_permission_missing_and_unexpected_statuses_refuse_without_retrying(status, needle):
    fake = FakeApi(ok_history()).script_for("/workflows/", (status, {}, b""))
    d, f, c = refused(fake)
    assert needle in d.message and len(f.requests) == 1 and c.waits == []


def test_a_redirect_is_never_followed_or_retried():
    fake = FakeApi(ok_history()).script_for("/workflows/", (302, {"Location": "https://evil.example/x"}, b""))
    d, f, _ = refused(fake)
    assert "HTTP 302" in d.message and f.hosts() == {"api.github.com"}


def test_a_rate_limit_with_retry_after_waits_exactly_that_long_then_continues():
    fake = FakeApi(ok_history()).script_for("/workflows/", (429, {"Retry-After": "7"}, b""), (403, {"Retry-After": "3"}, b""))
    d, f, c = decide(fake)
    assert not d.ok and "run 300" in d.message          # it got through to the real answer
    assert c.waits == [7.0, 3.0]


def test_a_rate_limit_without_retry_after_backs_off_and_x_ratelimit_reset_is_honored():
    fake = FakeApi(ok_history()).script_for("/workflows/", (429, {}, b""))
    _, _, c = decide(fake)
    assert c.waits == [2.0]
    fake = FakeApi(ok_history()).script_for("/workflows/", (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1700000004"}, b""))
    _, _, c = decide(fake, wall=lambda: 1_700_000_000.0)
    assert c.waits == [4.0]


def test_a_retry_after_longer_than_the_bound_refuses_at_once_without_waiting():
    fake = FakeApi(ok_history()).script_for("/workflows/", (429, {"Retry-After": "600"}, b""))
    d, f, c = refused(fake)
    assert "rate limited" in d.message and "600 s" in d.message and c.waits == [] and len(f.requests) == 1


def test_a_persistent_rate_limit_stops_after_the_bounded_attempts_and_the_total_wait_is_bounded():
    fake = FakeApi(ok_history()).script_for("/workflows/", *[(429, {"Retry-After": "60"}, b"")] * 5)
    d, f, c = refused(fake)
    assert "after 3 attempts" in d.message and len(f.requests) == 3 and sum(c.waits) <= 2 * run_guard.MAX_WAIT


@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_errors_are_retried_with_backoff_then_succeed(status):
    fake = FakeApi(ok_history()).script_for("/workflows/", (status, {}, b""), (status, {}, b""))
    d, f, c = decide(fake)
    assert not d.ok and "run 300" in d.message and c.waits == [2.0, 5.0]


def test_persistent_server_errors_refuse():
    fake = FakeApi(ok_history()).script_for("/workflows/", *[(503, {}, b"")] * 5)
    d, f, c = refused(fake)
    assert "HTTP 503" in d.message and "after 3 attempts" in d.message and c.waits == [2.0, 5.0]


def test_network_failures_are_retried_then_refuse():
    fake = FakeApi(ok_history()).script_for("/workflows/", OSError("connection reset"), TimeoutError("timed out"))
    d, f, c = decide(fake)
    assert not d.ok and "run 300" in d.message and c.waits == [2.0, 5.0]
    fake = FakeApi(ok_history()).script_for("/workflows/", *[ConnectionError("down")] * 5)
    d, f, c = refused(fake)
    assert "network error (ConnectionError)" in d.message and "down" not in d.message


def test_an_error_while_reading_a_jobs_list_refuses_too():
    for status in (401, 403, 404):
        fake = FakeApi(ok_history()).script_for("/runs/300/jobs", (status, {}, b""))
        refused(fake)
    fake = FakeApi(ok_history()).script_for("/runs/300/jobs", *[(500, {}, b"")] * 5)
    refused(fake)


def test_every_request_has_an_explicit_timeout_and_the_token_goes_only_to_the_api_host():
    d, f, _ = decide([published(300), me()])
    assert all(t == run_guard.REQUEST_TIMEOUT for _, _, t in f.requests) and run_guard.REQUEST_TIMEOUT <= 30
    assert all(h["Authorization"] == f"Bearer {TOKEN}" for _, h, _ in f.requests) and f.hosts() == {"api.github.com"}


def test_the_overall_time_budget_is_enforced():
    fake = FakeApi(ok_history()).script_for("/workflows/", *[(429, {"Retry-After": "40"}, b"")] * 5)
    d, _, c = refused(fake, deadline=50)
    assert "time budget" in d.message and c.t <= 50


@pytest.mark.parametrize("body,needle", [(b"<html>nope</html>", "malformed JSON"), (b"", "malformed JSON"), (b"\xff\xfe", "malformed JSON")])
def test_a_response_that_is_not_json_refuses(body, needle):
    fake = FakeApi(ok_history()).script_for("/workflows/", (200, {}, body))
    d, f, _ = refused(fake)
    assert needle in d.message and len(f.requests) == 1


def test_an_oversized_response_refuses():
    fake = FakeApi(ok_history()).script_for("/workflows/", (200, {}, b"x" * (run_guard.MAX_BODY + 1)))
    d, _, _ = refused(fake)
    assert "too large" in d.message


def test_a_pagination_link_that_points_elsewhere_is_never_followed():
    fake = FakeApi([preview(r) for r in (303, 302, 301)] + [me()], runs_per_page=2)
    orig = fake.__call__

    def evil(url, headers, timeout):
        s, h, b = orig(url, headers, timeout)
        if "Link" in h:
            h = {"Link": '<https://evil.example/steal?page=2>; rel="next"'}
        return s, h, b
    d, f, _ = decide(evil if False else _Wrap(fake, evil))
    assert not d.ok and "outside the API" in d.message
    assert all("evil.example" not in u for u, _, _ in fake.requests), "no request, and so no token, went to the other host"


class _Wrap(FakeApi):
    def __init__(self, inner, fn):
        self.__dict__.update(inner.__dict__)
        self._fn, self._inner = fn, inner

    def __call__(self, url, headers, timeout):
        return self._fn(url, headers, timeout)


# ---------- the token is never printed ----------

def run_main(monkeypatch, capsys, fake, *args, token=TOKEN, env=None):
    monkeypatch.setenv("GH_TOKEN", token)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = run_guard.main(["--repo", REPO, "--run-id", str(ME), *args], opener=fake, sleep=lambda s: None)
    return code, capsys.readouterr().out


@pytest.mark.parametrize("name,fake", [
    ("allowed", lambda: FakeApi([preview(300), me()])),
    ("blocked", lambda: FakeApi([published(300), me()])),
    ("401", lambda: FakeApi(ok_history()).script_for("/workflows/", (401, {}, TOKEN.encode()))),
    ("403", lambda: FakeApi(ok_history()).script_for("/workflows/", (403, {}, TOKEN.encode()))),
    ("429", lambda: FakeApi(ok_history()).script_for("/workflows/", *[(429, {"Retry-After": "300", "X-Echo": TOKEN}, b"")] * 3)),
    ("500", lambda: FakeApi(ok_history()).script_for("/workflows/", *[(500, {}, TOKEN.encode())] * 5)),
    ("network error that mentions the token", lambda: FakeApi(ok_history()).script_for("/workflows/", *[OSError(f"failed with {TOKEN}")] * 5)),
    ("body that is not JSON but contains the token", lambda: FakeApi(ok_history()).script_for("/workflows/", (200, {}, f"oops {TOKEN}".encode()))),
    ("redirect carrying the token", lambda: FakeApi(ok_history()).script_for("/workflows/", (302, {"Location": f"https://x.example/?t={TOKEN}"}, b""))),
    ("unexpected error", lambda: FakeApi(ok_history()).script_for("/workflows/", ValueError(f"bad {TOKEN}"))),
])
def test_the_token_never_appears_in_any_output(monkeypatch, capsys, name, fake):
    code, out = run_main(monkeypatch, capsys, fake())
    assert TOKEN not in out and "ghs_" not in out, out
    assert code in (0, 1) and out.strip()


@pytest.mark.parametrize("response", [(401, {}, TOKEN.encode()), (403, {}, TOKEN.encode()), (404, {}, TOKEN.encode()), (418, {}, TOKEN.encode()),
                                      (302, {"Location": f"https://x.example/?t={TOKEN}"}, b""), (200, {}, f"nope {TOKEN}".encode()),
                                      OSError(f"failed with {TOKEN}")])
def test_the_error_messages_themselves_never_carry_the_token_before_any_redaction(response):
    """The scrubbing in main() is a second line; the messages the checks build must already be free of the token."""
    fake = FakeApi(ok_history()).script_for("/workflows/", *[response] * 5)
    d, _, _ = decide(fake)
    assert not d.ok and TOKEN not in d.message


def test_api_messages_scrub_the_token_even_if_one_slipped_into_a_message():
    api = Api(TOKEN, BASE, opener=FakeApi([me()]))
    assert api.redact(f"x {TOKEN} y") == "x *** y"


# ---------- the command line, no first-release exemption ----------

def test_the_command_line_exit_codes(monkeypatch, capsys):
    code, out = run_main(monkeypatch, capsys, FakeApi([preview(300), me()]))
    assert code == 0 and out.startswith("ALLOWED")
    code, out = run_main(monkeypatch, capsys, FakeApi([published(300), me()]))
    assert code == 1 and out.startswith("REFUSED: run 300")
    code, out = run_main(monkeypatch, capsys, FakeApi([me()]).script_for("/workflows/", (404, {}, b"")))
    assert code == 1 and "could not be established safely" in out


def test_a_missing_token_or_invalid_arguments_refuse(monkeypatch, capsys):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert run_guard.main(["--repo", REPO, "--run-id", "1"], opener=FakeApi([me()])) == 1
    assert "GH_TOKEN is not set" in capsys.readouterr().out
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    for args in (["--repo", "no-slash", "--run-id", "1"], ["--repo", REPO, "--run-id", "0"], ["--repo", "a/b;rm", "--run-id", "1"],
                 ["--repo", REPO, "--run-id", "1", "--workflow", "../x"]):
        assert run_guard.main(args, opener=FakeApi([me()])) == 1
    capsys.readouterr()
    assert run_guard.main(["--repo", REPO, "--run-id", "1", "--api-url", "http://example.com"], opener=FakeApi([me()])) == 1
    assert "must be https" in capsys.readouterr().out


def test_the_token_is_only_ever_sent_over_https_except_to_a_local_test_server():
    for bad in ("http://example.com", "ftp://api.github.com", "api.github.com", "https://", "http://127.0.0.1.evil.example"):
        with pytest.raises(GuardError):
            Api(TOKEN, bad)
    for good in ("https://api.github.com", "http://127.0.0.1:8080", "http://localhost:9"):
        Api(TOKEN, good)


def test_there_is_no_first_release_exemption(monkeypatch, capsys):
    with pytest.raises(SystemExit):                                    # the option does not exist
        run_guard.main(["--repo", REPO, "--run-id", "1", "--first-release"])
    capsys.readouterr()
    with pytest.raises(SystemExit):
        run_guard.main(["--help"])
    assert "first" not in capsys.readouterr().out.lower()
    assert not any("first" in p for p in inspect.signature(run_guard.check).parameters) and not any("first" in p for p in inspect.signature(run_guard.main).parameters)
    assert "first_release" not in inspect.getsource(run_guard.check) and "FIRST_RELEASE" not in inspect.getsource(run_guard.main)
    # the environment variable the other guard reads changes nothing here: an API failure still refuses, and a clean history still allows
    code, _ = run_main(monkeypatch, capsys, FakeApi(ok_history()).script_for("/workflows/", (404, {}, b"")), env={"FIRST_RELEASE": "true"})
    assert code == 1
    code, _ = run_main(monkeypatch, capsys, FakeApi([me()]), env={"FIRST_RELEASE": "true"})
    assert code == 0


# ---------- a local HTTP stand-in, with the real client ----------

class Standin(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.server.seen.append((self.path, dict(self.headers)))
        status, headers, body = self.server.fake(f"http://127.0.0.1:{self.server.server_address[1]}{self.path}", dict(self.headers), 0)
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def standin():
    servers = []

    def start(history, **kw):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Standin)
        srv.seen = []
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        srv.fake = FakeApi(history, base=base, **kw)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return srv, base
    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


def real_main(monkeypatch, capsys, base):
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    code = run_guard.main(["--repo", REPO, "--run-id", str(ME), "--api-url", base], sleep=lambda s: None)
    return code, capsys.readouterr().out


def test_the_real_client_against_a_local_stand_in_blocks_allows_and_pages(standin, monkeypatch, capsys):
    srv, base = standin([preview(305), preview(304), published(303), preview(302), me(), published(150)], runs_per_page=2, jobs_per_page=1)
    code, out = real_main(monkeypatch, capsys, base)
    assert code == 1 and out.startswith("REFUSED: run 303")
    assert all(h.get("Authorization") == f"Bearer {TOKEN}" for _, h in srv.seen) and TOKEN not in out
    srv2, base2 = standin([preview(305), preview(304), me(), published(150)], runs_per_page=1)
    code, out = real_main(monkeypatch, capsys, base2)
    assert code == 0 and out.startswith("ALLOWED")
    assert any("page=2" in p for p, _ in srv2.seen), "the Link header of the real response was followed"


def test_the_real_client_handles_real_error_statuses(standin, monkeypatch, capsys):
    for status, headers in ((401, {}), (403, {}), (404, {}), (500, {}), (429, {"Retry-After": "999"})):
        srv, base = standin([me()])
        srv.fake.script_for("/workflows/", *[(status, headers, b"")] * 5)
        code, out = real_main(monkeypatch, capsys, base)
        assert code == 1 and out.startswith("REFUSED") and TOKEN not in out, (status, out)


def test_the_real_client_does_not_follow_a_redirect_and_never_sends_the_token_to_the_target(standin, monkeypatch, capsys):
    target, target_base = standin([me()])
    srv, base = standin([me()])
    srv.fake.script_for("/workflows/", (302, {"Location": f"{target_base}/stolen"}, b""))
    code, out = real_main(monkeypatch, capsys, base)
    assert code == 1 and "HTTP 302" in out
    assert target.seen == [], "the redirect target received no request, and so no token"


def test_the_real_client_refuses_a_connection_failure(monkeypatch, capsys):
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    code = run_guard.main(["--repo", REPO, "--run-id", str(ME), "--api-url", "http://127.0.0.1:9"], sleep=lambda s: None)
    out = capsys.readouterr().out
    assert code == 1 and "network error" in out and TOKEN not in out
