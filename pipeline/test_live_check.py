"""The checks around the deployment (pipeline/live_check.py): the stale-deployment guard and the live-site verification.

Nothing here talks to GitHub or to a real site. Responses come from a scripted fake with a fake clock, and a local server that
stands in for a deployed site (a scratch folder is the "deployment"). Made-up data only.
"""
from __future__ import annotations

import datetime as dt
import functools
import http.server
import json
import shutil
import threading
from pathlib import Path

import pytest

from pipeline import build, live_check, verify_site
from pipeline.config import load_config
from pipeline.test_timing import local_files

ROOT = Path(__file__).resolve().parent.parent
CFG = load_config(ROOT / "config" / "report.yaml")
NOW = dt.datetime(2026, 10, 25, 12, 0, 0, tzinfo=dt.timezone.utc)
URL = "https://example.invalid/site/"


def iso(minutes_ago: float) -> str:
    return (NOW - dt.timedelta(minutes=minutes_ago)).isoformat()


def report(refreshed_at: str, n: int = 3, tweak=None):
    """A made-up published report: (summary dict, data bytes, expected dict for verify_live)."""
    data = {"refreshed_at": refreshed_at, "rows": [{"lead_id": f"P{i:03d}", "closed": i % 2} for i in range(n)]}
    if tweak:
        tweak(data)
    raw = json.dumps(data, separators=(",", ":")).encode()
    version = live_check.version_of(raw)
    summary = {"refreshed_at": refreshed_at, "totals": {"leads": n}, "report": {"version": version, "data_file": "data.json", "data_bytes": len(raw), "rows": n}}
    return summary, raw, {"refreshed_at": refreshed_at, "version": version, "data_bytes": len(raw), "rows": n}


class Site:
    """A fake site. `serve(url)` -> (status, body); a function of time lets a test script cache lag and propagation."""

    def __init__(self, clock=None):
        self.clock = clock
        self.files: dict[str, tuple[int | None, bytes]] = {}
        self.timeline = None            # optional: callable(t) -> {path: (status, body)} overriding files
        self.requests: list[str] = []

    def put(self, summary=None, data=None, status=200):
        if summary is not None:
            self.files["summary.json"] = (status, summary if isinstance(summary, bytes) else json.dumps(summary).encode())
        if data is not None:
            self.files["data.json"] = (status, data)

    def __call__(self, url: str):
        self.requests.append(url)
        name = url.rsplit("/", 1)[-1]
        files = self.timeline(self.clock.t) if self.timeline else self.files
        return files.get(name, (404, b""))


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def verify(expected, site, clock, **kw):
    logged = []
    out = live_check.verify_live(expected, site, URL, clock=clock.now, sleep=clock.sleep, now=lambda: NOW + dt.timedelta(seconds=clock.t),
                                 log=logged.append, **kw)
    return out, logged


# ---------- the identity recipe ----------

def test_version_recipe_equals_the_builds_and_the_page_vectors():
    for data, want in {b"hello": "LPJNul-wow4m6DsqxbninhsW", b"": "47DEQpj8HBSa-_TImW-5JCeu", "é".encode(): "SplVfkAzw1Od4utlRyAXytX5"}.items():
        assert live_check.version_of(data) == want == build.report_version(data)


# ---------- the stale-deployment guard ----------

def decide(candidate: str, live_summary=None, *, status=200, first_release=False, body=None):
    site = Site()
    if body is not None:
        site.put(summary=body, status=status)
    elif live_summary is not None:
        site.put(summary=live_summary, status=status)
    else:
        site.files["summary.json"] = (status, b"")
    return live_check.guard(candidate, site, URL, first_release, now=NOW), site


def test_a_newer_candidate_is_allowed():
    d, site = decide(iso(1), report(iso(60))[0])
    assert d.ok and "newer than the live report" in d.message
    assert site.requests == [URL + "summary.json"]


def test_the_same_report_again_is_allowed():
    d, _ = decide(iso(5), report(iso(5))[0])
    assert d.ok and "same timestamp" in d.message


def test_an_older_candidate_is_refused_even_on_the_first_release_path():
    for first in (False, True):
        d, _ = decide(iso(60), report(iso(5))[0], first_release=first)
        assert not d.ok and "never deployed over a newer one" in d.message


@pytest.mark.parametrize("name,kwargs", [
    ("live site missing (404)", dict(status=404)),
    ("live site error (500)", dict(status=500)),
    ("network error", dict(status=None)),
    ("not JSON", dict(body=b"<html>oops</html>")),
    ("JSON array", dict(body=b"[1,2]")),
    ("no refreshed_at", dict(body=json.dumps({"totals": {}}).encode())),
    ("unparseable refreshed_at", dict(body=json.dumps({"refreshed_at": "soon"}).encode())),
    ("naive refreshed_at", dict(body=json.dumps({"refreshed_at": "2026-10-25T10:00:00"}).encode())),
    ("dated in the future", dict(live_summary=report(iso(-180))[0])),
])
def test_an_unreadable_or_untrusted_live_report_fails_closed_unless_it_is_the_first_release(name, kwargs):
    d, _ = decide(iso(1), **kwargs)
    assert not d.ok and d.message.startswith("REFUSED") and "first release" in d.message
    d, _ = decide(iso(1), first_release=True, **kwargs)
    assert d.ok and "ALLOWED without comparison" in d.message


def test_a_bad_candidate_is_always_refused():
    for cand in ("", "soon", "2026-10-25T10:00:00", iso(-180)):
        for first in (False, True):
            d, _ = decide(cand, report(iso(60))[0], first_release=first)
            assert not d.ok and d.message.startswith("REFUSED")


def test_known_limitation_a_stale_cdn_view_can_let_an_older_rerun_through():
    """Pinned on purpose (docs/publication_safety.md, "Known limitation"): the guard can only compare with what the CDN shows.
    If a newer report was deployed minutes ago and the CDN still serves an older one, an older run's re-run can slip through.
    If this test starts failing because the guard became stricter, update the documentation with it."""
    really_live = report(iso(5))[0]                     # deployed 5 minutes ago
    stale_edge = report(iso(300))[0]                    # what a lagging CDN copy still shows
    older_rerun = iso(60)                               # a re-run of an older run: newer than the stale view, older than the live report
    assert decide(older_rerun, stale_edge)[0].ok
    assert not decide(older_rerun, really_live)[0].ok   # refused as soon as the CDN shows the real live report


def test_the_guard_command_line_uses_the_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(live_check, "http_fetch", Site())
    just_now = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)).replace(microsecond=0).isoformat()      # the command uses the real clock
    assert live_check.main(["guard", "--url", URL, "--candidate-refreshed-at", just_now]) == 1
    assert live_check.main(["guard", "--url", URL, "--candidate-refreshed-at", just_now, "--first-release"]) == 0
    out = capsys.readouterr().out
    assert "REFUSED" in out and "ALLOWED" in out


# ---------- the live verification ----------

def test_a_matching_live_site_passes_at_once_and_the_data_is_hashed_independently():
    clock = Clock()
    summary, raw, expected = report(iso(1))
    site = Site(clock)
    site.put(summary, raw)
    out, _ = verify(expected, site, clock)
    assert out.ok and out.attempts == 1 and "hashes to the manifest" in out.message
    assert site.requests == [URL + "summary.json", URL + "data.json"]


def test_cache_lag_on_the_summary_is_waited_out():
    clock = Clock()
    old, old_raw, _ = report(iso(60))
    new, new_raw, expected = report(iso(1), n=5)
    site = Site(clock)
    site.timeline = lambda t: {"summary.json": (200, json.dumps(new if t >= 150 else old).encode()), "data.json": (200, new_raw if t >= 150 else old_raw)}
    out, logged = verify(expected, site, clock)
    assert out.ok and out.attempts == 6 and clock.t == 150
    assert any("still the older report" in l for l in logged)


def test_a_new_summary_beside_the_old_data_file_is_waited_out_never_accepted():
    clock = Clock()
    old, old_raw, _ = report(iso(60))
    new, new_raw, expected = report(iso(1), n=5)
    site = Site(clock)
    site.timeline = lambda t: {"summary.json": (200, json.dumps(new).encode()), "data.json": (200, new_raw if t >= 240 else old_raw)}
    out, logged = verify(expected, site, clock)
    assert out.ok and clock.t == 240
    assert any("does not hash to the manifest" in l for l in logged)


def test_a_missing_or_garbled_site_is_retried_then_succeeds():
    clock = Clock()
    summary, raw, expected = report(iso(1))
    site = Site(clock)
    states = [(404, b""), (500, b""), (200, b"<html>"), (None, b"")]
    def timeline(t):
        i = int(t // 30)
        if i < len(states):
            return {"summary.json": states[i], "data.json": (200, raw)}
        return {"summary.json": (200, json.dumps(summary).encode()), "data.json": (200, raw)}
    site.timeline = timeline
    out, logged = verify(expected, site, clock)
    assert out.ok and out.attempts == 5
    assert any("HTTP 404" in l for l in logged) and any("network error" in l for l in logged) and any("not JSON" in l for l in logged)


def test_a_site_that_never_catches_up_fails_after_about_twelve_minutes_and_says_the_deployment_may_have_happened():
    clock = Clock()
    old, old_raw, _ = report(iso(60))
    _, _, expected = report(iso(1))
    site = Site(clock)
    site.put(old, old_raw)
    out, _ = verify(expected, site, clock)
    assert not out.ok and 700 <= clock.t <= 720 and 24 <= out.attempts <= 25
    assert "THE DEPLOYMENT MAY ALREADY HAVE HAPPENED" in out.message
    assert "does not roll anything back" in out.message
    assert "rolled back" not in out.message and "restored" not in out.message


@pytest.mark.parametrize("name,make", [
    ("data altered but same size", lambda raw: raw.replace(b"P000", b"P999", 1)),
    ("data truncated", lambda raw: raw[:-5]),
    ("data not JSON", lambda raw: b"x" * len(raw)),
])
def test_live_data_that_does_not_match_the_manifest_is_never_accepted(name, make):
    clock = Clock()
    summary, raw, expected = report(iso(1))
    site = Site(clock)
    site.put(summary, make(raw))
    out, _ = verify(expected, site, clock, deadline_s=120)
    assert not out.ok and clock.t <= 120 and "MAY ALREADY HAVE HAPPENED" in out.message


def test_the_same_timestamp_with_a_different_report_stops_at_once():
    clock = Clock()
    summary, raw, expected = report(iso(1))
    other, other_raw, _ = report(iso(1), n=4)                      # same refreshed_at, different content
    site = Site(clock)
    site.put(other, other_raw)
    out, _ = verify(expected, site, clock)
    assert not out.ok and out.attempts == 1 and clock.t == 0 and "different or missing report version" in out.reason


def test_a_newer_live_report_means_the_candidate_was_superseded_and_stops_at_once():
    clock = Clock()
    _, _, expected = report(iso(10))
    newer, newer_raw, _ = report(iso(1))
    site = Site(clock)
    site.put(newer, newer_raw)
    out, _ = verify(expected, site, clock)
    assert not out.ok and out.attempts == 1 and "superseded" in out.reason


def test_a_manifest_missing_from_the_live_summary_is_a_mismatch():
    clock = Clock()
    summary, raw, expected = report(iso(1))
    legacy = {k: v for k, v in summary.items() if k != "report"}
    site = Site(clock)
    site.put(legacy, raw)
    out, _ = verify(expected, site, clock)
    assert not out.ok and out.attempts == 1


def test_a_future_dated_live_summary_is_not_trusted():
    clock = Clock()
    _, _, expected = report(iso(1))
    far, far_raw, _ = report(iso(-600))
    site = Site(clock)
    site.put(far, far_raw)
    out, logged = verify(expected, site, clock, deadline_s=60)
    assert not out.ok and any("untrusted" in l for l in logged)


# ---------- a simulated deployment, end to end, with the real command line ----------

class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


@pytest.fixture
def served(tmp_path):
    root = tmp_path / "live"
    root.mkdir()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(root)))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield root, f"http://127.0.0.1:{srv.server_address[1]}/"
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture(scope="module")
def two_sites(tmp_path_factory):
    """Two real builds (older, newer) from made-up sources, standing for two published reports."""
    mp = pytest.MonkeyPatch()
    try:
        tmp = tmp_path_factory.mktemp("two_sites")
        (tmp / "src").mkdir()
        files = local_files(tmp / "src", CFG)
        mp.setattr(build, "fetch_baseline", lambda url: None)
        (tmp / "map.json").write_text(json.dumps(files))
        sites = []
        for name in ("old", "new"):
            assert build.main(["--out", str(tmp / name), "--local-files", str(tmp / "map.json"), "--preview"]) == 0
            sites.append(tmp / name / "site")
    finally:
        mp.undo()
    # the second report is a later one: re-time it consistently so its manifest still matches its bytes
    old_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).replace(microsecond=0).isoformat()
    for s, at in ((sites[0], old_at), (sites[1], None)):
        if at:
            d = json.loads((s / "data.json").read_bytes())
            d["refreshed_at"] = at
            raw = json.dumps(d, separators=(",", ":")).encode()
            (s / "data.json").write_bytes(raw)
            sm = json.loads((s / "summary.json").read_text(encoding="utf-8"))
            sm.update(refreshed_at=at)
            sm["report"].update(version=build.report_version(raw), data_bytes=len(raw))
            (s / "summary.json").write_text(json.dumps(sm, indent=2), encoding="utf-8")
    return sites


def info(site):
    r = verify_site.verify(site, CFG, source_index=ROOT / "site" / "index.html", vendor_sha256=(ROOT / "site" / "vendor" / "SHA256SUMS").read_text().split()[0])
    assert r.ok, r.problems
    return r


def deploy(site, live_root):
    """The simulated deployment: replace the served folder's contents with the site's files."""
    for p in list(live_root.iterdir()):
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    for name in ("summary.json", "data.json"):
        shutil.copy2(site / name, live_root / name)


def test_simulated_deployment_guard_then_deploy_then_verify(two_sites, served, capsys):
    old, new = two_sites
    live_root, url = served
    o, n = info(old), info(new)
    assert o.refreshed_at < n.refreshed_at
    # nothing is live yet: refused, except on the explicit first-release path
    assert live_check.main(["guard", "--url", url, "--candidate-refreshed-at", o.refreshed_at]) == 1
    assert live_check.main(["guard", "--url", url, "--candidate-refreshed-at", o.refreshed_at, "--first-release"]) == 0
    deploy(old, live_root)
    args = lambda i: ["verify", "--url", url, "--refreshed-at", i.refreshed_at, "--version", i.version, "--data-bytes", str(i.data_bytes), "--rows", str(i.rows),
                      "--deadline", "1", "--interval", "0.2"]
    assert live_check.main(args(o)) == 0                                                      # the deployed report verifies
    assert live_check.main(args(n)) == 1                                                      # the candidate is not live yet
    assert live_check.main(["guard", "--url", url, "--candidate-refreshed-at", n.refreshed_at]) == 0       # newer: allowed
    deploy(new, live_root)
    assert live_check.main(args(n)) == 0
    assert live_check.main(["guard", "--url", url, "--candidate-refreshed-at", o.refreshed_at]) == 1       # older over newer: refused
    assert live_check.main(["guard", "--url", url, "--candidate-refreshed-at", n.refreshed_at]) == 0       # the same report again: allowed
    out = capsys.readouterr().out
    assert "REFUSED" in out and "LIVE CHECK PASSED" in out and "MAY ALREADY HAVE HAPPENED" in out


def test_simulated_post_deployment_mismatch_is_reported_not_hidden(two_sites, served, capsys):
    old, new = two_sites
    live_root, url = served
    n = info(new)
    deploy(new, live_root)
    (live_root / "data.json").write_bytes((live_root / "data.json").read_bytes().replace(b"P", b"Q", 1))      # altered in transit
    rc = live_check.main(["verify", "--url", url, "--refreshed-at", n.refreshed_at, "--version", n.version, "--data-bytes", str(n.data_bytes),
                          "--rows", str(n.rows), "--deadline", "1", "--interval", "0.2"])
    out = capsys.readouterr().out
    assert rc == 1 and "does not hash to the manifest" in out and "MAY ALREADY HAVE HAPPENED" in out and "does not roll anything back" in out


def test_recovery_drill_on_a_simulated_site(two_sites, served):
    """The documented recovery steps, run against scratch folders only (no deployment, nothing real is touched)."""
    old, new = two_sites
    live_root, url = served
    deploy(new, live_root)                                                 # a bad-but-valid newer report is "live"
    known_good = info(old)                                                 # step 1: the recovery copy verifies from disk
    assert live_check.read_summary(live_check.http_fetch, url, dt.datetime.now(dt.timezone.utc))[0] == "ok"
    # step 2: the guard refuses an older report over the newer one, by design (restoring an exact older report is not automated)
    assert live_check.guard(known_good.refreshed_at, live_check.http_fetch, url, now=dt.datetime.now(dt.timezone.utc)).ok is False
    # step 3: rolling forward with a newer report is allowed, and the same report can be redeployed
    later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=1)).replace(microsecond=0).isoformat()
    assert live_check.guard(later, live_check.http_fetch, url, now=dt.datetime.now(dt.timezone.utc)).ok
    assert live_check.guard(info(new).refreshed_at, live_check.http_fetch, url, now=dt.datetime.now(dt.timezone.utc)).ok
