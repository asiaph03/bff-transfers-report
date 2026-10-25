"""The report manifest the dashboard polls, and the page's update check (made-up data only).

summary.json carries report = {version, data_file, data_bytes, rows}. The version is a hash of the exact bytes of data.json,
so the page can tell a new report from an old one and verify what it downloads. The page logic itself is tested in
tests_js/ (Node's built-in runner, no dependencies); this file runs that suite as well when Node is installed.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from pipeline import build, validate
from pipeline.config import load_config
from pipeline.test_timing import local_files

ROOT = Path(__file__).resolve().parent.parent
CFG_PATH = ROOT / "config" / "report.yaml"

# The same vectors are asserted in tests_js/poll.test.mjs against the page's own code.
VECTORS = {b"hello": "LPJNul-wow4m6DsqxbninhsW", b"": "47DEQpj8HBSa-_TImW-5JCeu",
           b'{"rows":[]}': "FjOQLGy7ped3Db7Rct91SiUH", "é".encode("utf-8"): "SplVfkAzw1Od4utlRyAXytX5"}


def test_report_version_matches_the_shared_vectors_and_the_documented_recipe():
    for data, want in VECTORS.items():
        assert build.report_version(data) == want
        assert want == base64.urlsafe_b64encode(hashlib.sha256(data).digest()[:18]).decode()
        assert re.fullmatch(r"[A-Za-z0-9_-]{24}", want)


def test_the_version_follows_the_bytes():
    assert build.report_version(b'{"rows":[1]}') != build.report_version(b'{"rows":[2]}')
    assert build.report_version(b"x") == build.report_version(b"x")


def test_a_version_can_never_trip_the_privacy_check():
    """A hex digest would trip the phone-number pattern about 8% of the time and randomly block a publication."""
    for i in range(20_000):
        v = build.report_version(os.urandom(16))
        assert not validate.PHONE_LIKE.search(v) and not validate.EMAIL_LIKE.search(v), v


@pytest.fixture(scope="module")
def good_files(tmp_path_factory):
    return local_files(tmp_path_factory.mktemp("poll_src"), load_config(CFG_PATH))


def run_build(tmp_path: Path, monkeypatch, files: dict, name: str, at: dt.datetime | None = None) -> Path:
    monkeypatch.setattr(build, "fetch_baseline", lambda url: None)               # no network in tests
    if at is not None:
        class Clock:                                                             # a stand-in for the datetime module with a fixed "now"
            timezone = dt.timezone
            class datetime:                                                      # noqa: N801
                @staticmethod
                def now(tz=None):
                    return at
        monkeypatch.setattr(build, "dt", Clock)
    d = tmp_path / name
    d.mkdir()
    (d / "map.json").write_text(json.dumps(files))
    assert build.main(["--out", str(d / "out"), "--local-files", str(d / "map.json"), "--preview"]) == 0
    return d / "out" / "site"


def test_the_published_manifest_describes_exactly_the_published_data(tmp_path, monkeypatch, good_files):
    site = run_build(tmp_path, monkeypatch, good_files, "a")
    summary = json.loads((site / "summary.json").read_text(encoding="utf-8"))
    raw = (site / "data.json").read_bytes()
    data = json.loads(raw)
    rep = summary["report"]
    assert set(rep) == {"version", "data_file", "data_bytes", "rows"}
    assert rep["data_file"] == "data.json" and (site / rep["data_file"]).exists()
    assert rep["version"] == build.report_version(raw)                          # the hash of the bytes actually on disk
    assert rep["data_bytes"] == len(raw) and rep["rows"] == len(data["rows"])
    assert summary["refreshed_at"] == data["refreshed_at"]


def test_the_rest_of_summary_json_is_unchanged(tmp_path, monkeypatch, good_files):
    site = run_build(tmp_path, monkeypatch, good_files, "keys")
    summary = json.loads((site / "summary.json").read_text(encoding="utf-8"))
    assert list(summary) == ["refreshed_at", "window", "totals", "warnings", "stale_after_days", "config_version", "report"]
    assert list(summary["totals"])[:3] == ["leads", "connected", "converted"]    # still what fetch_baseline() reads


def test_published_data_keeps_the_same_fields_and_no_new_public_data(tmp_path, monkeypatch, good_files):
    site = run_build(tmp_path, monkeypatch, good_files, "fields")
    cfg = load_config(CFG_PATH)
    rows = json.loads((site / "data.json").read_text(encoding="utf-8"))["rows"]
    assert all(list(r) == cfg["publish"]["fields"] for r in rows)               # exactly the allowlist, nothing added
    page = (site / "index.html").read_text(encoding="utf-8")
    assert page == (ROOT / "site" / "index.html").read_text(encoding="utf-8")


def test_a_new_publication_has_a_new_version_and_the_same_data_has_the_same_one(tmp_path, monkeypatch, good_files):
    t1, t2 = dt.datetime(2026, 10, 25, 10, 0, 0, tzinfo=dt.timezone.utc), dt.datetime(2026, 10, 25, 10, 30, 0, tzinfo=dt.timezone.utc)
    a = json.loads((run_build(tmp_path, monkeypatch, good_files, "t1", at=t1) / "summary.json").read_text())
    b = json.loads((run_build(tmp_path, monkeypatch, good_files, "t1b", at=t1) / "summary.json").read_text())
    c = json.loads((run_build(tmp_path, monkeypatch, good_files, "t2", at=t2) / "summary.json").read_text())
    assert a["report"] == b["report"]                                            # same data, same moment: same identity
    assert a["report"]["version"] != c["report"]["version"]                      # a later refresh is a new report
    assert a["report"]["data_bytes"] == c["report"]["data_bytes"], "same shape of data, so only the hash tells them apart"


def test_a_failed_build_publishes_no_manifest(tmp_path, monkeypatch, good_files):
    monkeypatch.setattr(build, "fetch_baseline", lambda url: None)
    files = dict(good_files)
    del files["cordoba"]
    d = tmp_path / "fail"
    d.mkdir()
    (d / "map.json").write_text(json.dumps(files))
    assert build.main(["--out", str(d / "out"), "--local-files", str(d / "map.json"), "--preview"]) == 1
    assert not (d / "out" / "site").exists()


# ---------- the page ----------

def test_the_documented_polling_numbers_are_the_ones_in_the_page():
    html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
    defaults = dict(re.findall(r"^\s+(\w+): (\d+),", html.split("const DEFAULTS = {", 1)[1].split("};", 1)[0], flags=re.M))
    doc = (ROOT / "docs" / "dashboard_polling.md").read_text(encoding="utf-8")
    assert defaults["intervalMs"] == "60000" and "60 seconds" in doc
    assert defaults["maxBackoffMs"] == "600000" and "10 minutes" in doc
    assert defaults["requestTimeoutMs"] == "10000" and "10 seconds" in doc
    assert "retryDelaysMs: [5000, 10000, 20000]" in html and "5, 10 and 20 seconds" in doc


def test_the_page_makes_requests_only_to_its_own_files():
    html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
    fetched = re.findall(r"""fetch\(\s*['"]([^'"]+)['"]""", html)
    assert sorted(set(fetched)) == ["data.json", "summary.json"]
    for url in re.findall(r"base \+ '([^']+)'", html):
        assert url.split("?")[0] in {"summary.json", "data.json"}


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed; the page logic tests need it")
def test_the_page_logic_tests_pass():
    """Runs tests_js/ (polling logic and data layer, with a fake network, clock and page) with Node's built-in runner."""
    r = subprocess.run(["node", "--test", "tests_js/poll.test.mjs", "tests_js/dash.test.mjs"], cwd=ROOT, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=300)       # node prints UTF-8 whatever the console code page is
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
    assert re.search(r"\bfail 0\b", r.stdout) and not re.search(r"\bpass 0\b", r.stdout) and re.search(r"\btests (\d+)", r.stdout)
    assert int(re.search(r"\btests (\d+)", r.stdout).group(1)) >= 40, "the JavaScript suite ran far fewer tests than expected"
