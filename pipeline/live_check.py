"""Checks around the deployment, using only the standard library (the deploy job installs nothing).

    python -m pipeline.live_check guard  --url URL --candidate-refreshed-at TIME [--first-release]
    python -m pipeline.live_check verify --url URL --refreshed-at TIME --version V --data-bytes N --rows N

Pass values as --name=value (--version=-Abc...): a report version can start with "-", which argparse would take for an option.

guard   runs BEFORE deploy-pages. It refuses to deploy an older report over a newer one and fails closed when the live
        report cannot be read or trusted, except on the explicitly selected first-release path.
verify  runs AFTER deploy-pages. It asks the live site, for up to about 12 minutes (the CDN may keep serving old files),
        whether it now serves the candidate report: same timestamp and version in summary.json, and data.json whose own
        SHA-256 matches the manifest. It cannot undo a deployment; a failure says plainly that the deployment may already
        have happened.

Messages name times, versions and counts, never values from the report.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import http.client
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

MAX_FUTURE = dt.timedelta(minutes=10)
MAX_BODY = 8_000_000
Fetcher = Callable[[str], "tuple[int | None, bytes]"]       # url -> (HTTP status or None when the network failed, body)


def version_of(data: bytes) -> str:
    """Report identity: first 18 bytes of the SHA-256, 24 base64url characters (must equal pipeline.build.report_version)."""
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()[:18]).decode("ascii")


def parse_time(value) -> dt.datetime | None:
    """A timezone-aware ISO 8601 time, or None."""
    if not isinstance(value, str):
        return None
    try:
        t = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return t if t.tzinfo is not None else None


def http_fetch(url: str, timeout: float = 20.0) -> "tuple[int | None, bytes]":
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache", "Pragma": "no-cache", "User-Agent": "bff-live-check"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(MAX_BODY + 1)
            return (r.status, body) if len(body) <= MAX_BODY else (None, b"")
    except urllib.error.HTTPError as e:
        return e.code, b""
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
        return None, b""


def _base(url: str) -> str:
    return url if url.endswith("/") else url + "/"


def read_summary(fetch: Fetcher, base: str, now: dt.datetime):
    """('ok', summary_dict) or (reason, None). Reasons: unreachable, unreadable, future-dated."""
    status, body = fetch(_base(base) + "summary.json")
    if status != 200:
        return (f"unreachable (HTTP {status})" if status else "unreachable (network error)"), None
    try:
        s = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "unreadable (not JSON)", None
    t = parse_time(s.get("refreshed_at")) if isinstance(s, dict) else None
    if t is None:
        return "unreadable (no valid refreshed_at)", None
    if t > now + MAX_FUTURE:
        return "untrusted (refreshed_at is in the future)", None
    return "ok", s


@dataclass
class Decision:
    ok: bool
    message: str


def guard(candidate_refreshed_at: str, fetch: Fetcher, base: str, first_release: bool = False,
          now: dt.datetime | None = None) -> Decision:
    """May this candidate be deployed over what is live? Equal is allowed (the same report again); older is refused."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cand = parse_time(candidate_refreshed_at)
    if cand is None:
        return Decision(False, "REFUSED: the candidate report has no valid refreshed_at, so it cannot be compared with the live report.")
    if cand > now + MAX_FUTURE:
        return Decision(False, f"REFUSED: the candidate report is dated {candidate_refreshed_at}, in the future; it would make every later report look older.")
    state, live = read_summary(fetch, base, now)
    if state != "ok":
        if first_release:
            return Decision(True, f"ALLOWED without comparison: the live report is {state}, and this run was started as the first release.")
        return Decision(False, f"REFUSED: the live report is {state}, so the candidate cannot be compared with it and nothing may be deployed "
                               "without that comparison (only a run explicitly started as the first release skips it).")
    live_at = parse_time(live["refreshed_at"])
    if live_at > cand:
        return Decision(False, f"REFUSED: the live report ({live['refreshed_at']}) is newer than the candidate ({candidate_refreshed_at}). "
                               "An older report is never deployed over a newer one.")
    if live_at == cand:
        return Decision(True, f"ALLOWED: the live report has the same timestamp as the candidate ({candidate_refreshed_at}); this redeploys the same report.")
    return Decision(True, f"ALLOWED: the candidate ({candidate_refreshed_at}) is newer than the live report ({live['refreshed_at']}).")


@dataclass
class Outcome:
    ok: bool
    message: str
    attempts: int
    reason: str


def _check_once(expected: dict, fetch: Fetcher, base: str, now: dt.datetime):
    """(code, reason). code: ok | retry | stop. A stop is a state that waiting cannot fix."""
    state, live = read_summary(fetch, base, now)
    if state != "ok":
        return "retry", f"summary.json {state}"
    want_at = parse_time(expected["refreshed_at"])
    live_at = parse_time(live["refreshed_at"])
    if live_at < want_at:
        return "retry", f"the live summary.json is still the older report ({live['refreshed_at']}): cached or not yet propagated"
    if live_at > want_at:
        return "stop", f"a newer report ({live['refreshed_at']}) is live, so this candidate cannot be verified (it was superseded)"
    rep = live.get("report")
    if not isinstance(rep, dict) or rep.get("version") != expected["version"]:
        return "stop", "the live summary.json has the candidate's timestamp but a different or missing report version"
    status, body = fetch(_base(base) + "data.json")
    if status != 200:
        return "retry", f"data.json unreachable (HTTP {status})" if status else "data.json unreachable (network error)"
    got = version_of(body)
    if got != rep["version"] or got != expected["version"]:
        return "retry", "the live data.json does not hash to the manifest version (older file still cached, or altered)"
    if len(body) != expected["data_bytes"] or rep.get("data_bytes") != expected["data_bytes"]:
        return "retry", "the live data.json size does not match the manifest"
    try:
        d = json.loads(body.decode("utf-8"))
        rows = len(d["rows"])
        same_time = d["refreshed_at"] == expected["refreshed_at"]
    except (UnicodeDecodeError, ValueError, KeyError, TypeError):
        return "retry", "the live data.json cannot be read"
    if rows != expected["rows"] or not same_time:
        return "retry", "the live data.json rows or timestamp do not match the manifest"
    return "ok", "ok"


def verify_live(expected: dict, fetch: Fetcher, base: str, *, deadline_s: float = 720.0, interval_s: float = 30.0,
                sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                now: Callable[[], dt.datetime] | None = None, log: Callable[[str], None] = print) -> Outcome:
    """Wait, within the deadline, for the live site to serve `expected` (refreshed_at, version, data_bytes, rows)."""
    now = now or (lambda: dt.datetime.now(dt.timezone.utc))
    start, attempts, reason = clock(), 0, ""
    while True:
        attempts += 1
        code, reason = _check_once(expected, fetch, base, now())
        if code == "ok":
            return Outcome(True, f"LIVE CHECK PASSED after {attempts} attempt(s): the live site serves the candidate report "
                                 f"(refreshed_at {expected['refreshed_at']}, version {expected['version']}); data.json hashes to the manifest "
                                 f"({expected['data_bytes']} bytes, {expected['rows']} rows). This is what this check saw; other visitors' "
                                 "caches can lag by up to about 10 minutes.", attempts, "ok")
        log(f"  attempt {attempts}: {reason}")
        if code == "stop" or clock() - start + interval_s > deadline_s:
            break
        sleep(interval_s)
    waited = clock() - start
    return Outcome(False,
                   f"LIVE CHECK FAILED after {attempts} attempt(s) over {waited:.0f} s: {reason}.\n"
                   "THE DEPLOYMENT MAY ALREADY HAVE HAPPENED. This check does not roll anything back and cannot tell whether the live site is "
                   "serving the new report, the previous one, or a mix of files from both. Look at summary.json and data.json on the site "
                   "directly, and see docs/publication_safety.md for what to do next.", attempts, reason)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Checks before and after the deployment (standard library only).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("guard")
    g.add_argument("--url", required=True)
    g.add_argument("--candidate-refreshed-at", required=True)
    g.add_argument("--first-release", action="store_true")
    v = sub.add_parser("verify")
    v.add_argument("--url", required=True)
    v.add_argument("--refreshed-at", required=True)
    v.add_argument("--version", required=True)
    v.add_argument("--data-bytes", required=True, type=int)
    v.add_argument("--rows", required=True, type=int)
    v.add_argument("--deadline", type=float, default=720.0, help="seconds to keep trying (default 720, about 12 minutes)")
    v.add_argument("--interval", type=float, default=30.0)
    a = ap.parse_args(argv)
    if a.cmd == "guard":
        d = guard(a.candidate_refreshed_at, http_fetch, a.url, a.first_release)
        print(d.message)
        return 0 if d.ok else 1
    out = verify_live({"refreshed_at": a.refreshed_at, "version": a.version, "data_bytes": a.data_bytes, "rows": a.rows},
                      http_fetch, a.url, deadline_s=a.deadline, interval_s=a.interval)
    print(out.message)
    return 0 if out.ok else 1


if __name__ == "__main__":
    sys.exit(main())
