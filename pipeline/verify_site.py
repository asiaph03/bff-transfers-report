"""Final check of the files a build wrote, before anything can be packaged or deployed.

    python -m pipeline.verify_site out/site [--github-output FILE] [--summary-file FILE]

The build checks its numbers in memory, then writes the site. This module reads the written files back, from disk, and
checks what would actually be published: exactly the expected set of files, a report manifest that describes the bytes of
data.json, matching timestamps, only the approved public fields, and no personal data or secrets. Any problem means the
site must not be uploaded. Messages name files, fields and counts, never values from the report.

It also owns the only deletion the build performs: remove_build_output() deletes the files this build writes and nothing
else, so an unexpected file in the output folder is never removed.
"""
from __future__ import annotations

import argparse
import codecs
import datetime as dt
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .live_check import parse_time, version_of      # recomputed from the bytes, not taken from build.py
from .sheets import _SECRETISH
from .validate import Report, check_privacy

ROOT = Path(__file__).resolve().parent.parent

VENDOR_FILE = "vendor/d3.v7.8.5.min.js"
EXPECTED_FILES = frozenset({"index.html", "data.json", "summary.json", ".nojekyll", VENDOR_FILE})
EXPECTED_DIRS = frozenset({"vendor"})
DATA_KEYS = {"refreshed_at", "rows"}
SUMMARY_KEYS = {"refreshed_at", "window", "totals", "warnings", "stale_after_days", "config_version", "report"}
REPORT_KEYS = {"version", "data_file", "data_bytes", "rows"}
VERSION_RE = re.compile(r"^[A-Za-z0-9_-]{24}$")
MAX_FUTURE = dt.timedelta(minutes=10)     # a report dated further ahead than this would make every later report look older


@dataclass
class Verified:
    problems: list[str] = field(default_factory=list)
    refreshed_at: str = ""
    version: str = ""
    data_bytes: int = 0
    rows: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems


def _reject_constant(name):
    raise ValueError(f"{name} is not valid JSON")


def _load_json(raw: bytes, name: str, problems: list[str]):
    """The parsed document, or None after recording why not (empty, byte order mark, not UTF-8, not JSON, NaN)."""
    if not raw:
        problems.append(f"{name}: empty")
        return None, ""
    if raw.startswith(codecs.BOM_UTF8):
        problems.append(f"{name}: starts with a byte order mark")
        return None, ""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        problems.append(f"{name}: not valid UTF-8")
        return None, ""
    try:
        return json.loads(text, parse_constant=_reject_constant), text
    except ValueError:
        problems.append(f"{name}: not valid JSON (truncated or malformed)")
        return None, text


def verify(site, cfg: dict, *, source_index: Path | None = None, vendor_sha256: str | None = None,
           now: dt.datetime | None = None) -> Verified:
    """Check a built site folder. `cfg` supplies the approved public fields. `source_index` is the page the build copied
    (compared after line-ending normalisation); `vendor_sha256` is the recorded checksum of the chart library."""
    out = Verified()
    p = out.problems
    site = Path(site)
    if site.is_symlink() or not site.is_dir():
        p.append(f"{site.name}: not a folder")
        return out

    # 1. exactly the expected files, nothing else
    found: dict[str, Path] = {}
    for path in sorted(site.rglob("*")):
        rel = path.relative_to(site).as_posix()
        if path.is_symlink():
            p.append(f"unexpected symbolic link: {rel[:80]}")
        elif path.is_dir():
            if rel not in EXPECTED_DIRS:
                p.append(f"unexpected folder: {rel[:80]}")
        elif rel not in EXPECTED_FILES:
            p.append(f"unexpected file: {rel[:80]}")
        else:
            found[rel] = path
    for rel in sorted(EXPECTED_FILES - set(found)):
        p.append(f"missing file: {rel}")

    now = now or dt.datetime.now(dt.timezone.utc)
    data = summary = None
    raw = b""
    texts: dict[str, str] = {}

    # 2. data.json
    if "data.json" in found:
        raw = found["data.json"].read_bytes()
        data, texts["data.json"] = _load_json(raw, "data.json", p)
    rows: list = []
    if data is not None:
        if not isinstance(data, dict) or set(data) != DATA_KEYS:
            p.append("data.json: top-level keys are not exactly refreshed_at and rows")
        else:
            rows = data["rows"]
            if not isinstance(rows, list) or not rows:
                p.append("data.json: rows is not a non-empty list")
                rows = []
            if parse_time(data["refreshed_at"]) is None:
                p.append("data.json: refreshed_at is not a timezone-aware time")
    fields = cfg["publish"]["fields"]
    if rows:
        bad_fields = sum(1 for r in rows if not isinstance(r, dict) or list(r.keys()) != fields)
        if bad_fields:
            p.append(f"data.json: {bad_fields} row(s) do not have exactly the approved public fields in order")
        else:
            ids = [r["lead_id"] for r in rows]
            if len(set(map(str, ids))) != len(ids):
                p.append("data.json: lead ids are not unique")

    # 3. summary.json and the manifest
    if "summary.json" in found:
        summary, texts["summary.json"] = _load_json(found["summary.json"].read_bytes(), "summary.json", p)
    if summary is not None:
        if not isinstance(summary, dict) or set(summary) != SUMMARY_KEYS:
            p.append("summary.json: keys are not exactly the expected set")
        else:
            at = parse_time(summary["refreshed_at"])
            if at is None:
                p.append("summary.json: refreshed_at is not a timezone-aware time")
            elif at > now + MAX_FUTURE:
                p.append("summary.json: refreshed_at is in the future")
            if isinstance(data, dict) and summary["refreshed_at"] != data.get("refreshed_at"):
                p.append("summary.json and data.json: refreshed_at differs")
            out.refreshed_at = summary["refreshed_at"] if isinstance(summary["refreshed_at"], str) else ""
            totals = summary["totals"]
            if rows and isinstance(totals, dict):
                try:
                    if totals.get("leads") != len(rows):
                        p.append("summary.json: totals.leads does not equal the number of rows")
                    if totals.get("closed") != sum(r["closed"] for r in rows):
                        p.append("summary.json: totals.closed does not equal the closed rows")
                except (TypeError, KeyError):
                    p.append("summary.json: totals cannot be reconciled with the rows")
            rep = summary["report"]
            if not isinstance(rep, dict) or set(rep) != REPORT_KEYS:
                p.append("summary.json: report manifest keys are not exactly version, data_file, data_bytes, rows")
            else:
                if rep["data_file"] != "data.json":
                    p.append("report manifest: data_file is not data.json")
                if not isinstance(rep["version"], str) or not VERSION_RE.match(rep["version"]):
                    p.append("report manifest: version is not 24 base64url characters")
                elif raw and rep["version"] != version_of(raw):
                    p.append("report manifest: version is not the hash of the data.json bytes")
                if raw and (type(rep["data_bytes"]) is not int or rep["data_bytes"] != len(raw)):
                    p.append("report manifest: data_bytes does not equal the size of data.json")
                if rows and (type(rep["rows"]) is not int or rep["rows"] != len(rows)):
                    p.append("report manifest: rows does not equal the number of rows in data.json")
                if isinstance(rep["version"], str):
                    out.version = rep["version"]
                out.data_bytes = rep["data_bytes"] if type(rep["data_bytes"]) is int else 0
                out.rows = rep["rows"] if type(rep["rows"]) is int else 0

    # 4. the page and the chart library
    if "index.html" in found:
        page = found["index.html"].read_bytes()
        try:
            texts["index.html"] = page.decode("utf-8")
        except UnicodeDecodeError:
            p.append("index.html: not valid UTF-8")
        if not page:
            p.append("index.html: empty")
        elif source_index is not None:
            want = Path(source_index).read_bytes().replace(b"\r\n", b"\n")
            if page.replace(b"\r\n", b"\n") != want:
                p.append("index.html: differs from the page in the repository (truncated or altered)")
    if vendor_sha256 and VENDOR_FILE in found and hashlib.sha256(found[VENDOR_FILE].read_bytes()).hexdigest() != vendor_sha256:
        p.append(f"{VENDOR_FILE}: does not match its recorded checksum")

    # 5. privacy and secrets, on what is actually in the files
    scan = dict(texts)
    if data is not None and summary is not None:                      # check_privacy parses the JSON files, so only when they parsed
        privacy = Report()
        check_privacy(privacy, scan)
        p.extend(privacy.failures)                                    # "name: check: N found": names and counts only
    for name, text in scan.items():
        if _SECRETISH.search(text):
            p.append(f"{name}: looks like it contains a private key")
    return out


def remove_build_output(site) -> list[str]:
    """Delete the files a build writes into `site` (and the folders left empty), nothing else. Returns the names of anything
    unexpected that was left untouched: an empty list means the folder is gone or was ours alone."""
    site = Path(site)
    if not (site.exists() or site.is_symlink()):
        return []
    if site.is_symlink() or not site.is_dir():
        return [site.name]
    left: list[str] = []
    try:
        paths = sorted(site.rglob("*"), key=lambda q: len(q.parts), reverse=True)    # deepest first
    except OSError:
        return [site.name]                                                            # cannot even list it: touch nothing
    for path in paths:
        rel = path.relative_to(site).as_posix()
        if path.is_symlink():
            left.append(rel)
        elif path.is_dir():
            if rel in EXPECTED_DIRS:
                try:
                    path.rmdir()
                except OSError:
                    pass                                                              # still holds something unexpected
            else:
                left.append(rel)
        elif rel in EXPECTED_FILES:
            try:
                path.unlink()
            except OSError:                                                           # locked, read-only: report it, never raise
                if path.exists():
                    left.append(rel)
        else:
            left.append(rel)
    if not left:
        try:
            site.rmdir()
        except OSError:
            left.append(site.name)
    return sorted(set(left))


def discard_build_output(site, log=print) -> None:
    """remove_build_output() for error paths: best effort, and it never replaces the failure being handled.

    A cleanup problem is logged and swallowed, so the original exception or interrupt is what the caller re-raises and the run
    still exits non-zero. Anything left behind is never uploaded: the steps that upload need this run to have succeeded.
    Only Exception is caught; a second interrupt during cleanup is not hidden."""
    try:
        left = remove_build_output(site)
    except Exception as e:                      # noqa: BLE001 - the failure already being handled matters more than this one
        log(f"warning: could not remove the partial build output ({type(e).__name__}); nothing is uploaded from a failed run")
        return
    if left:
        log(f"warning: {len(left)} item(s) in {Path(site).name}/ could not be removed or are not files of this build; left as they are. "
            "Nothing is uploaded from a failed run")


def _write_outputs(info: Verified, github_output: str | None, summary_file: str | None) -> None:
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            for k, v in (("refreshed_at", info.refreshed_at), ("version", info.version), ("data_bytes", info.data_bytes), ("rows", info.rows)):
                f.write(f"{k}={v}\n")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write("\n### Candidate report (verified)\n\n"
                    f"- refreshed_at: `{info.refreshed_at}`\n- version: `{info.version}`\n- rows: {info.rows}\n- data.json bytes: {info.data_bytes}\n"
                    "- The `site-preview` artifact of this run holds exactly these files; use the version above to identify it.\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Verify a built site folder before it is uploaded.")
    ap.add_argument("site")
    ap.add_argument("--config", default=str(ROOT / "config" / "report.yaml"))
    ap.add_argument("--source-index", default=str(ROOT / "site" / "index.html"))
    ap.add_argument("--vendor-sums", default=str(ROOT / "site" / "vendor" / "SHA256SUMS"))
    ap.add_argument("--github-output", help="append refreshed_at, version, data_bytes and rows (key=value) to this file")
    ap.add_argument("--summary-file", help="append a short markdown note about the candidate report to this file")
    args = ap.parse_args(argv)
    from .config import ConfigError, load_config

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"config is not valid: {e}")
        return 1
    sums = Path(args.vendor_sums)
    vendor = sums.read_text().split()[0] if sums.is_file() else None
    info = verify(args.site, cfg, source_index=Path(args.source_index), vendor_sha256=vendor)
    if not info.ok:
        print(f"SITE NOT VERIFIED: {len(info.problems)} problem(s). Nothing may be uploaded or deployed from {args.site}.")
        for x in info.problems:
            print(f"- {x}")
        return 1
    print(f"site verified: {len(EXPECTED_FILES)} files, {info.rows} rows, {info.data_bytes} bytes, refreshed_at {info.refreshed_at}, version {info.version}")
    _write_outputs(info, args.github_output, args.summary_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
