"""The final check of a built site (pipeline/verify_site.py), the build's use of it, and the cleanup rules (made-up data only).

A built site is read back from disk and must be exactly the expected files, a manifest that describes the bytes of data.json,
matching timestamps, only the approved public fields, and no personal data or secrets. Anything else must stop the run before
anything is uploaded; the last-known-good published files are never touched by a failing build.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from pipeline import build, rules, verify_site
from pipeline.config import load_config
from pipeline.test_timing import local_files

ROOT = Path(__file__).resolve().parent.parent
CFG_PATH = ROOT / "config" / "report.yaml"
CFG = load_config(CFG_PATH)
SOURCE_INDEX = ROOT / "site" / "index.html"
VENDOR_SHA = (ROOT / "site" / "vendor" / "SHA256SUMS").read_text().split()[0]


@pytest.fixture(scope="module")
def good_files(tmp_path_factory):
    return local_files(tmp_path_factory.mktemp("verify_src"), CFG)


def build_main(tmp_path: Path, monkeypatch, files: dict, name: str = "out", *flags: str, baseline=None):
    monkeypatch.setattr(build, "fetch_baseline", lambda url: baseline)            # no network in tests
    m = tmp_path / f"{name}.map.json"
    m.write_text(json.dumps(files))
    out = tmp_path / name
    return build.main(["--out", str(out), "--local-files", str(m), *flags]), out


@pytest.fixture(scope="module")
def built(tmp_path_factory, good_files):
    """One real build, made once; tests copy it before changing anything."""
    mp = pytest.MonkeyPatch()
    try:
        tmp = tmp_path_factory.mktemp("verify_built")
        rc, out = build_main(tmp, mp, good_files, "out", "--preview")
    finally:
        mp.undo()
    assert rc == 0
    return out / "site"


@pytest.fixture
def site(built, tmp_path):
    dest = tmp_path / "site"
    shutil.copytree(built, dest)
    return dest


def check(site_dir, **kw):
    kw.setdefault("source_index", SOURCE_INDEX)
    kw.setdefault("vendor_sha256", VENDOR_SHA)
    return verify_site.verify(site_dir, CFG, **kw)


def rewrite(site_dir: Path, mutate_data=None, mutate_summary=None, refresh_manifest: bool = True):
    """Change data.json and/or summary.json; by default the manifest is recomputed so only the intended problem remains."""
    dp, sp = site_dir / "data.json", site_dir / "summary.json"
    data, summary = json.loads(dp.read_bytes()), json.loads(sp.read_text(encoding="utf-8"))
    if mutate_data:
        mutate_data(data)
    raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
    dp.write_bytes(raw)
    if refresh_manifest:
        summary["report"].update(version=build.report_version(raw), data_bytes=len(raw), rows=len(data["rows"]))
    if mutate_summary:
        mutate_summary(summary)
    sp.write_text(json.dumps(summary, indent=2), encoding="utf-8")


def problems(result) -> str:
    return " | ".join(result.problems)


# ---------- the good site ----------

def test_a_real_build_verifies_and_reports_the_candidate(site):
    r = check(site)
    assert r.ok, r.problems
    raw = (site / "data.json").read_bytes()
    summary = json.loads((site / "summary.json").read_text(encoding="utf-8"))
    assert (r.refreshed_at, r.version, r.data_bytes, r.rows) == (summary["refreshed_at"], build.report_version(raw), len(raw), len(json.loads(raw)["rows"]))


def test_the_independent_version_recipe_equals_the_builds():
    for data in (b"", b"hello", b'{"rows":[]}', "é".encode()):
        assert verify_site.version_of(data) == build.report_version(data)


# ---------- truncated or tampered data ----------

def test_truncated_data_is_rejected(site):
    raw = (site / "data.json").read_bytes()
    (site / "data.json").write_bytes(raw[: len(raw) // 2])
    r = check(site)
    assert not r.ok
    assert "data.json: not valid JSON" in problems(r) and "data_bytes" in problems(r) and "version is not the hash" in problems(r)


def test_tampered_data_of_the_same_size_fails_on_the_hash_alone(site):
    raw = bytearray((site / "data.json").read_bytes())
    i = next(i for i in range(len(raw) // 2, len(raw)) if chr(raw[i]).isdigit())
    raw[i] = ord("0") if raw[i] != ord("0") else ord("1")
    (site / "data.json").write_bytes(bytes(raw))
    r = check(site)
    assert not r.ok
    assert problems(r) == "report manifest: version is not the hash of the data.json bytes"


@pytest.mark.parametrize("name,payload,expect", [
    ("empty", b"", "data.json: empty"),
    ("byte order mark", b"\xef\xbb\xbf{}", "byte order mark"),
    ("not utf-8", b"\xff\xfe\x00{", "not valid UTF-8"),
    ("NaN", b'{"refreshed_at":"2026-01-01T00:00:00+00:00","rows":[NaN]}', "not valid JSON"),
    ("an array", b"[1,2,3]", "top-level keys"),
])
def test_unreadable_data_files_are_rejected(site, name, payload, expect):
    (site / "data.json").write_bytes(payload)
    r = check(site)
    assert not r.ok and expect in problems(r), problems(r)


# ---------- the manifest ----------

@pytest.mark.parametrize("name,mutate,expect", [
    ("wrong version", lambda s: s["report"].update(version="A" * 24), "version is not the hash"),
    ("malformed version", lambda s: s["report"].update(version="short"), "version is not 24 base64url"),
    ("byte count off by one", lambda s: s["report"].update(data_bytes=s["report"]["data_bytes"] + 1), "data_bytes does not equal"),
    ("byte count as text", lambda s: s["report"].update(data_bytes=str(s["report"]["data_bytes"])), "data_bytes does not equal"),
    ("row count off by one", lambda s: s["report"].update(rows=s["report"]["rows"] - 1), "rows does not equal"),
    ("row count as a boolean", lambda s: s["report"].update(rows=True), "rows does not equal"),
    ("other data file", lambda s: s["report"].update(data_file="../x.json"), "data_file is not data.json"),
    ("extra manifest key", lambda s: s["report"].update(extra=1), "manifest keys are not exactly"),
    ("missing manifest key", lambda s: s["report"].pop("rows"), "manifest keys are not exactly"),
    ("manifest not an object", lambda s: s.update(report="x"), "manifest keys are not exactly"),
    ("manifest absent", lambda s: s.pop("report"), "keys are not exactly the expected set"),
    ("extra summary key", lambda s: s.update(extra=1), "keys are not exactly the expected set"),
])
def test_a_wrong_manifest_is_rejected(site, name, mutate, expect):
    rewrite(site, mutate_summary=mutate, refresh_manifest=False)
    r = check(site)
    assert not r.ok and expect in problems(r), problems(r)


# ---------- timestamps ----------

def test_timestamps_must_match(site):
    rewrite(site, mutate_summary=lambda s: s.update(refreshed_at="2020-01-01T00:00:00+00:00"))
    assert "refreshed_at differs" in problems(check(site))


@pytest.mark.parametrize("value,expect", [("2026-10-24T10:00:00", "not a timezone-aware time"), ("yesterday", "not a timezone-aware time"), (12345, "not a timezone-aware time")])
def test_unreadable_or_naive_timestamps_are_rejected(site, value, expect):
    def both(d):
        d["refreshed_at"] = value
    rewrite(site, mutate_data=both, mutate_summary=lambda s: s.update(refreshed_at=value))
    assert expect in problems(check(site))


def test_a_report_dated_in_the_future_is_rejected(site):
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)).replace(microsecond=0).isoformat()
    def both(d):
        d["refreshed_at"] = future
    rewrite(site, mutate_data=both, mutate_summary=lambda s: s.update(refreshed_at=future))
    assert "refreshed_at is in the future" in problems(check(site))
    assert check(site, now=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)).ok       # only because the clock says so


# ---------- files ----------

def test_an_extra_file_or_folder_is_rejected(site):
    (site / "notes.txt").write_text("x")
    (site / "extra").mkdir()
    (site / "vendor" / "other.js").write_text("x")
    r = check(site)
    assert "unexpected file: notes.txt" in problems(r) and "unexpected folder: extra" in problems(r) and "unexpected file: vendor/other.js" in problems(r)


@pytest.mark.parametrize("rel", ["index.html", "data.json", "summary.json", ".nojekyll", "vendor/d3.v7.8.5.min.js"])
def test_a_missing_file_is_rejected(site, rel):
    (site / rel).unlink()
    assert f"missing file: {rel}" in problems(check(site))


def test_a_symbolic_link_is_rejected(site):
    try:
        os.symlink(site / "data.json", site / "link.json")
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available here")
    assert "unexpected symbolic link" in problems(check(site))


def test_the_page_and_the_chart_library_must_be_the_ones_in_the_repository(site):
    (site / "index.html").write_bytes((site / "index.html").read_bytes()[:-50])
    (site / "vendor" / "d3.v7.8.5.min.js").write_bytes(b"alert(1)")
    r = check(site)
    assert "index.html: differs from the page in the repository" in problems(r) and "does not match its recorded checksum" in problems(r)
    (site / "index.html").write_bytes(b"")
    assert "index.html: empty" in problems(check(site))


# ---------- public fields and rows ----------

@pytest.mark.parametrize("name,mutate,expect", [
    ("an extra field", lambda d: [r.update(extra="x") for r in d["rows"]], "approved public fields"),
    ("a missing field", lambda d: [r.pop("payout") for r in d["rows"]], "approved public fields"),
    ("fields reordered", lambda d: d.update(rows=[dict(reversed(list(r.items()))) for r in d["rows"]]), "approved public fields"),
    ("one row with a field too many", lambda d: d["rows"][0].update(phone="x"), "approved public fields"),
    ("duplicate lead ids", lambda d: d["rows"][1].update(lead_id=d["rows"][0]["lead_id"]), "lead ids are not unique"),
    ("no rows", lambda d: d.update(rows=[]), "rows is not a non-empty list"),
    ("rows not a list", lambda d: d.update(rows={}), "rows is not a non-empty list"),
    ("extra top-level key", lambda d: d.update(extra=1), "top-level keys"),
])
def test_rows_must_have_exactly_the_public_fields(site, name, mutate, expect):
    rewrite(site, mutate_data=mutate)
    r = check(site)
    assert not r.ok and expect in problems(r), problems(r)


def test_totals_must_agree_with_the_rows(site):
    rewrite(site, mutate_summary=lambda s: s["totals"].update(leads=s["totals"]["leads"] + 1, closed=s["totals"]["closed"] + 1))
    assert "totals.leads does not equal" in problems(check(site)) and "totals.closed does not equal" in problems(check(site))


# ---------- privacy and secrets ----------

def test_a_phone_number_in_the_data_is_rejected(site):
    rewrite(site, mutate_data=lambda d: d["rows"][0].update(agent="call 5551234567 now"))
    assert "data.json: no phone-number-like values" in problems(check(site))


def test_an_email_address_in_the_summary_is_rejected(site):
    rewrite(site, mutate_summary=lambda s: s.update(warnings=["write to someone@example.com"]))
    assert "summary.json: no email addresses" in problems(check(site))


@pytest.mark.parametrize("secret", ['"private_key": "x"', "-----BEGIN PRIVATE KEY-----"])
def test_a_secret_anywhere_is_rejected(site, secret):
    rewrite(site, mutate_summary=lambda s: s.update(warnings=[secret]))
    r = check(site)
    assert "summary.json: looks like it contains a private key" in problems(r) or "summary.json: no credentials" in problems(r)   # JSON escaping hides one marker from the other check
    (site / "index.html").write_text("<html>" + secret + "</html>", encoding="utf-8")
    assert "index.html: looks like it contains a private key" in problems(check(site, source_index=None))


def test_messages_never_contain_values_from_the_report(site):
    rewrite(site, mutate_data=lambda d: d["rows"][0].update(agent="call 5551234567 now"))
    text = problems(check(site))
    assert "5551234567" not in text and "call 5551234567" not in text


# ---------- the command line ----------

def test_the_command_line_passes_a_good_site_and_writes_the_outputs(site, tmp_path, capsys):
    gh, summary = tmp_path / "gh.txt", tmp_path / "summary.md"
    rc = verify_site.main([str(site), "--github-output", str(gh), "--summary-file", str(summary)])
    assert rc == 0 and "site verified" in capsys.readouterr().out
    lines = dict(l.split("=", 1) for l in gh.read_text().splitlines())
    assert set(lines) == {"refreshed_at", "version", "data_bytes", "rows"} and len(lines["version"]) == 24
    assert lines["version"] in summary.read_text() and "site-preview" in summary.read_text()


def test_the_command_line_fails_a_bad_site_and_writes_no_outputs(site, tmp_path, capsys):
    (site / "data.json").write_bytes(b"{")
    gh = tmp_path / "gh.txt"
    rc = verify_site.main([str(site), "--github-output", str(gh)])
    out = capsys.readouterr().out
    assert rc == 1 and "SITE NOT VERIFIED" in out and "Nothing may be uploaded or deployed" in out
    assert not gh.exists()
    assert verify_site.main([str(tmp_path / "missing")]) == 1


# ---------- deleting only what the build wrote ----------

def test_remove_build_output_deletes_our_files_and_the_folder(site):
    assert verify_site.remove_build_output(site) == []
    assert not site.exists()
    assert verify_site.remove_build_output(site) == []                       # nothing there is fine


def test_remove_build_output_leaves_anything_else_alone(site):
    (site / "mine.txt").write_text("keep")
    (site / "vendor" / "mine.js").write_text("keep")
    (site / "sub").mkdir()
    (site / "sub" / "deep.txt").write_text("keep")
    left = verify_site.remove_build_output(site)
    assert set(left) >= {"mine.txt", "vendor/mine.js", "sub", "sub/deep.txt"}
    assert (site / "mine.txt").read_text() == "keep" and (site / "vendor" / "mine.js").read_text() == "keep" and (site / "sub" / "deep.txt").exists()
    assert not (site / "data.json").exists() and not (site / "summary.json").exists() and not (site / "index.html").exists()


def test_remove_build_output_never_follows_a_link(tmp_path):
    target = tmp_path / "precious"
    target.mkdir()
    (target / "data.json").write_text("keep")
    link = tmp_path / "site"
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available here")
    assert verify_site.remove_build_output(link) == ["site"]
    assert (target / "data.json").read_text() == "keep"


# ---------- the build: verification, interrupts, last-known-good ----------

def test_the_build_verifies_what_it_wrote_and_says_so(tmp_path, monkeypatch, good_files):
    rc, out = build_main(tmp_path, monkeypatch, good_files, "ok", "--preview")
    report = (out / "validation.md").read_text(encoding="utf-8")
    assert rc == 0 and "published files verified after writing" in report and "site verification: passed" in report
    assert verify_site.verify(out / "site", CFG, source_index=SOURCE_INDEX, vendor_sha256=VENDOR_SHA).ok


def short_data_json(monkeypatch):
    orig = Path.write_bytes
    monkeypatch.setattr(Path, "write_bytes", lambda self, data: orig(self, data[: len(data) // 2] if self.name == "data.json" else data))


def test_a_short_data_json_is_caught_before_anything_can_be_uploaded(tmp_path, monkeypatch, good_files):
    short_data_json(monkeypatch)
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "unrelated.txt").write_text("keep")
    rc, out = build_main(tmp_path, monkeypatch, good_files, "out", "--preview")
    assert rc == 1 and not (out / "site").exists()
    assert "published files verified after writing" in (out / "validation.md").read_text(encoding="utf-8")
    assert (out / "unrelated.txt").read_text() == "keep"


def test_an_interrupt_leaves_no_half_written_site_and_no_unrelated_deletions(tmp_path, monkeypatch, good_files):
    def boom(*a, **k):
        raise KeyboardInterrupt()
    monkeypatch.setattr(build.shutil, "copy2", boom)
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "unrelated.txt").write_text("keep")
    with pytest.raises(KeyboardInterrupt):
        build_main(tmp_path, monkeypatch, good_files, "out", "--preview")
    assert not (tmp_path / "out" / "site").exists()
    assert (tmp_path / "out" / "unrelated.txt").read_text() == "keep"


def test_an_unexpected_error_leaves_no_site_and_keeps_unrelated_files(tmp_path, monkeypatch, good_files):
    monkeypatch.setattr(rules, "to_public_rows", lambda *a, **k: (_ for _ in ()).throw(KeyError("x")))
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "unrelated.txt").write_text("keep")
    rc, out = build_main(tmp_path, monkeypatch, good_files, "out", "--preview")
    assert rc == 1 and not (out / "site").exists() and (out / "unrelated.txt").read_text() == "keep"


def test_stale_build_output_is_replaced_but_a_foreign_file_stops_the_build_untouched(tmp_path, monkeypatch, good_files):
    old = tmp_path / "out" / "site"
    old.mkdir(parents=True)
    (old / "data.json").write_text('{"old": true}')                         # ours (stale): cleared
    rc, out = build_main(tmp_path, monkeypatch, good_files, "out", "--preview")
    assert rc == 0 and json.loads((out / "site" / "data.json").read_bytes())["rows"]
    shutil.rmtree(out)
    (out / "site").mkdir(parents=True)
    (out / "site" / "notes.txt").write_text("not ours")
    rc, out = build_main(tmp_path, monkeypatch, good_files, "out", "--preview")
    assert rc == 1
    assert (out / "site" / "notes.txt").read_text() == "not ours"
    assert not (out / "site" / "data.json").exists()
    assert "output folder holds only files this build writes" in (out / "validation.md").read_text(encoding="utf-8")


def fingerprint(folder: Path) -> dict:
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.rglob("*")) if p.is_file()}


def _totals(built):
    return json.loads((built / "summary.json").read_text(encoding="utf-8"))["totals"]


@pytest.mark.parametrize("scenario", ["source_missing", "too_few_rows", "baseline_breach", "baseline_unavailable", "unexpected_error",
                                      "short_data_json", "interrupt"])
def test_the_last_known_good_files_survive_every_pre_deployment_failure(scenario, tmp_path, monkeypatch, good_files, built):
    live = tmp_path / "live"                                                  # stands for what is published right now
    shutil.copytree(built, live)
    before = fingerprint(live)
    files, flags, baseline, exc = dict(good_files), (), None, None
    if scenario == "source_missing":
        del files["cordoba"]
    elif scenario == "too_few_rows":
        small = tmp_path / "ringba_small.csv"
        small.write_text("\n".join(Path(good_files["ringba"]).read_text(encoding="utf-8").splitlines()[:11]) + "\n", encoding="utf-8")
        files["ringba"] = str(small)
    elif scenario == "baseline_breach":
        baseline = {k: v * 20 + 100000 for k, v in _totals(built).items() if isinstance(v, (int, float)) and k in CFG["validation"]["baseline"]["metrics"]}
    elif scenario == "unexpected_error":
        monkeypatch.setattr(rules, "to_public_rows", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    elif scenario == "short_data_json":
        short_data_json(monkeypatch)
        baseline = {k: _totals(built)[k] for k in CFG["validation"]["baseline"]["metrics"]}
    elif scenario == "interrupt":
        monkeypatch.setattr(build.shutil, "copy2", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
        baseline = {k: _totals(built)[k] for k in CFG["validation"]["baseline"]["metrics"]}
        exc = KeyboardInterrupt
    if scenario in ("source_missing", "too_few_rows", "unexpected_error"):
        flags = ("--preview",)
    if exc:
        with pytest.raises(exc):
            build_main(tmp_path, monkeypatch, files, "out", *flags, baseline=baseline)
        out = tmp_path / "out"
    else:
        rc, out = build_main(tmp_path, monkeypatch, files, "out", *flags, baseline=baseline)
        assert rc == 1
    assert not (out / "site").exists(), "nothing is left to upload"
    assert fingerprint(live) == before, "the published files were not touched"


# ---------- baseline and first release (unchanged behavior) ----------

def test_baseline_defaults_and_first_release_behavior_are_unchanged(tmp_path, monkeypatch, good_files, built):
    assert CFG["validation"]["baseline"]["required"] is True
    totals = _totals(built)
    ok = {k: totals[k] for k in CFG["validation"]["baseline"]["metrics"]}
    rc, out = build_main(tmp_path, monkeypatch, good_files, "a", baseline=ok)
    assert rc == 0 and (out / "site").exists()                                                  # a baseline exists, no first release: publishes
    rc, out = build_main(tmp_path, monkeypatch, good_files, "b", baseline=None)
    assert rc == 1 and not (out / "site").exists()                                              # no baseline, not a first release: refused
    assert "baseline available" in (out / "validation.md").read_text(encoding="utf-8")
    rc, out = build_main(tmp_path, monkeypatch, good_files, "c", "--first-release", baseline=None)
    assert rc == 0 and (out / "site").exists()                                                  # the explicit first-release path
    rc, out = build_main(tmp_path, monkeypatch, good_files, "d", "--first-release", baseline=ok)
    assert rc == 1 and not (out / "site").exists()                                              # first release only once
    assert "first_release used only once" in (out / "validation.md").read_text(encoding="utf-8")


# ---------- cleanup is best effort: it never masks the failure being handled ----------

def unlink_fails(monkeypatch, names=None):
    """Make deleting files fail (a locked or read-only file), for the given file names or for every file."""
    orig = Path.unlink

    def failing(self, *a, **k):
        if names is None or self.name in names:
            raise PermissionError("locked")
        return orig(self, *a, **k)
    monkeypatch.setattr(Path, "unlink", failing)


def test_a_file_that_cannot_be_deleted_is_reported_not_raised(site, monkeypatch):
    unlink_fails(monkeypatch, {"data.json"})
    left = verify_site.remove_build_output(site)                   # does not raise
    assert left == ["data.json"] or "data.json" in left
    assert (site / "data.json").exists() and not (site / "summary.json").exists() and not (site / "index.html").exists()
    monkeypatch.undo()
    assert verify_site.remove_build_output(site) == [] and not site.exists()


def test_discard_build_output_never_raises_for_an_ordinary_error_and_says_what_happened(site, monkeypatch):
    seen = []
    monkeypatch.setattr(verify_site, "remove_build_output", lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
    assert verify_site.discard_build_output(site, log=seen.append) is None
    assert len(seen) == 1 and "warning" in seen[0] and "RuntimeError" in seen[0] and "nothing is uploaded" in seen[0]
    monkeypatch.undo()
    unlink_fails(monkeypatch)
    seen.clear()
    verify_site.discard_build_output(site, log=seen.append)
    assert len(seen) == 1 and "could not be removed" in seen[0]


def test_discard_build_output_does_not_hide_a_second_interrupt(site, monkeypatch):
    monkeypatch.setattr(verify_site, "remove_build_output", lambda s: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        verify_site.discard_build_output(site, log=lambda m: None)


def test_an_interrupt_is_reraised_unchanged_when_cleanup_also_fails(tmp_path, monkeypatch, good_files, capsys):
    monkeypatch.setattr(build.shutil, "copy2", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    unlink_fails(monkeypatch)
    with pytest.raises(KeyboardInterrupt):                          # the original interrupt, not a PermissionError
        build_main(tmp_path, monkeypatch, good_files, "out", "--preview")
    assert "warning:" in capsys.readouterr().out
    left = tmp_path / "out" / "site"
    assert left.exists()                                            # the partial output could not be removed ...
    assert not verify_site.verify(left, CFG, source_index=SOURCE_INDEX, vendor_sha256=VENDOR_SHA).ok     # ... and is not a publishable site


def test_an_unexpected_error_is_still_reported_and_fails_the_run_when_cleanup_also_fails(tmp_path, monkeypatch, good_files):
    monkeypatch.setattr(build.shutil, "copy2", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    unlink_fails(monkeypatch)
    rc, out = build_main(tmp_path, monkeypatch, good_files, "out", "--preview")
    assert rc == 1
    assert "build completed: unexpected OSError" in (out / "validation.md").read_text(encoding="utf-8")        # the original cause is what is reported
    assert not verify_site.verify(out / "site", CFG, source_index=SOURCE_INDEX, vendor_sha256=VENDOR_SHA).ok


def test_a_failed_verification_is_still_a_failed_run_when_cleanup_also_fails(tmp_path, monkeypatch, good_files):
    short_data_json(monkeypatch)
    unlink_fails(monkeypatch)
    rc, out = build_main(tmp_path, monkeypatch, good_files, "out", "--preview")
    assert rc == 1
    assert "published files verified after writing" in (out / "validation.md").read_text(encoding="utf-8")
    assert not verify_site.verify(out / "site", CFG, source_index=SOURCE_INDEX, vendor_sha256=VENDOR_SHA).ok


def cleanup_breaks_after_start(monkeypatch):
    """The start-of-run cleanup works; every later call (the error handlers) raises a non-OSError, as a bug in cleanup would."""
    real, calls = verify_site.remove_build_output, {"n": 0}

    def flaky(site):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("cleanup bug")
        return real(site)
    monkeypatch.setattr(verify_site, "remove_build_output", flaky)


def test_whatever_breaks_inside_cleanup_the_original_failure_is_what_comes_out(tmp_path, monkeypatch, good_files, capsys):
    # an interrupt is re-raised as the interrupt
    cleanup_breaks_after_start(monkeypatch)
    monkeypatch.setattr(build.shutil, "copy2", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        build_main(tmp_path, monkeypatch, good_files, "a", "--preview")
    # an unexpected error is reported as that error
    monkeypatch.undo()
    cleanup_breaks_after_start(monkeypatch)
    monkeypatch.setattr(build.shutil, "copy2", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    rc, out = build_main(tmp_path, monkeypatch, good_files, "b", "--preview")
    assert rc == 1 and "build completed: unexpected OSError" in (out / "validation.md").read_text(encoding="utf-8")
    # a failed verification is still a failed run
    monkeypatch.undo()
    cleanup_breaks_after_start(monkeypatch)
    short_data_json(monkeypatch)
    rc, out = build_main(tmp_path, monkeypatch, good_files, "c", "--preview")
    assert rc == 1 and "published files verified after writing" in (out / "validation.md").read_text(encoding="utf-8")
    assert capsys.readouterr().out.count("warning: could not remove the partial build output (RuntimeError)") >= 3     # each handler said so


def test_a_failure_to_write_the_report_does_not_hide_the_original_error(tmp_path, monkeypatch, good_files, capsys):
    monkeypatch.setattr(rules, "to_public_rows", lambda *a, **k: (_ for _ in ()).throw(KeyError("x")))
    monkeypatch.setattr(build, "finish", lambda *a, **k: (_ for _ in ()).throw(OSError("cannot write validation.md")))
    rc, out = build_main(tmp_path, monkeypatch, good_files, "out", "--preview")
    assert rc == 1 and "build failed: unexpected KeyError" in capsys.readouterr().out and not (out / "site").exists()
