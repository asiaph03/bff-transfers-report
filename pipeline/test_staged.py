"""Staged ingestion must change nothing the report can show (made-up data only).

The flag is off by default. These tests hold the staged flow to the default flow on: the published files, the
validation report's checks (failures, warnings, passes, in order) for good runs and for every kind of failing run,
and the reduced tables themselves. They also pin the fallbacks and the fail-closed behaviour.
"""
from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pandas as pd
import pytest
import yaml

from benchmarks.synth import generate
from pipeline import build, rules
from pipeline import staged as staged_mod
from pipeline.config import load_config, validate_config
from pipeline.sheets import SourceError, read_all
from pipeline.test_timing import local_files

CFG_PATH = Path(__file__).resolve().parent.parent / "config" / "report.yaml"


@pytest.fixture(scope="module")
def cfg():
    return load_config(CFG_PATH)


@pytest.fixture(scope="module")
def good_files(tmp_path_factory, cfg):
    return local_files(tmp_path_factory.mktemp("staged_src"), cfg)


def copy_files(files: dict, where: Path) -> dict:
    where.mkdir(parents=True, exist_ok=True)
    out = {}
    for k, p in files.items():
        dst = where / Path(p).name
        shutil.copy2(p, dst)
        out[k] = str(dst)
    return out


def edit(path: str, fn) -> None:
    p = Path(path)
    p.write_text(fn(p.read_text(encoding="utf-8")), encoding="utf-8")


def run_build(tmp_path: Path, monkeypatch, files: dict, name: str, *flags: str):
    monkeypatch.setattr(build, "fetch_baseline", lambda url: None)           # no network in tests
    d = tmp_path / name
    d.mkdir()
    (d / "map.json").write_text(json.dumps(files))
    rc = build.main(["--out", str(d / "out"), "--local-files", str(d / "map.json"), "--preview", *flags])
    text = (d / "out" / "validation.md").read_text(encoding="utf-8")
    site = d / "out" / "site"
    pub = None
    if site.exists():
        data, summary = json.loads((site / "data.json").read_text()), json.loads((site / "summary.json").read_text())
        data.pop("refreshed_at"), summary.pop("refreshed_at")
        pub = (data, summary, (site / "index.html").read_bytes())
    return rc, text, pub


def checks_of(report: str) -> str:
    """Failures, warnings and passed checks, in the order written (the timing facts above them legitimately differ)."""
    return report.split("## Failures", 1)[1]


# ---------- the flag ----------

def test_flag_is_off_by_default_and_must_be_a_boolean(cfg):
    assert cfg["ingest"] == {"staged": False}
    for bad in ({"staged": "yes"}, {}, {"staged": 1}, "on"):
        c = copy.deepcopy(cfg)
        c["ingest"] = bad
        assert any("ingest.staged" in p for p in validate_config(c)), bad
    c = copy.deepcopy(cfg)
    del c["ingest"]
    assert not validate_config(c)                                    # the key is optional; absent means off


def test_the_default_run_never_touches_the_staged_code(tmp_path, monkeypatch, good_files):
    def boom(*a, **k):
        raise AssertionError("staged ingestion must not run unless asked for")

    monkeypatch.setattr(staged_mod, "ingest", boom)
    rc, text, pub = run_build(tmp_path, monkeypatch, good_files, "default")
    assert rc == 0 and pub is not None
    assert "staged ingestion" not in text                              # the default report has no new line


# ---------- same published files and same checks ----------

def test_staged_and_default_publish_identical_files_and_checks(tmp_path, monkeypatch, good_files):
    rc_f, text_f, pub_f = run_build(tmp_path, monkeypatch, good_files, "full", "--ingest", "full")
    rc_s, text_s, pub_s = run_build(tmp_path, monkeypatch, good_files, "staged", "--ingest", "staged")
    assert rc_f == rc_s == 0
    assert pub_f == pub_s and pub_f[1]["totals"]["closed"] > 0         # data.json, summary.json, index.html
    assert checks_of(text_f) == checks_of(text_s) and "**Result:** PASSED" in text_s
    assert "- staged ingestion: used" in text_s and "staged ingestion" not in text_f


def test_the_config_flag_turns_it_on_like_the_command_line(tmp_path, monkeypatch, good_files, cfg):
    raw = yaml.safe_load(CFG_PATH.read_text(encoding="utf-8"))
    raw["ingest"]["staged"] = True
    alt = tmp_path / "staged_on.yaml"
    alt.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    rc, text, pub = run_build(tmp_path, monkeypatch, good_files, "viaconfig", "--config", str(alt))
    _, _, pub_f = run_build(tmp_path, monkeypatch, good_files, "full2", "--ingest", "full")
    assert rc == 0 and "- staged ingestion: used" in text and pub == pub_f
    rc2, text2, _ = run_build(tmp_path, monkeypatch, good_files, "forced_off", "--config", str(alt), "--ingest", "full")
    assert rc2 == 0 and "staged ingestion" not in text2                 # the command line can still force the default flow


# ---------- failing runs fail the same way and report the same checks ----------

def damage_few_paid_leads(files):
    edit(files["ringba"], lambda t: "\n".join(t.splitlines()[:1100]) + "\n")     # enough rows, too few paid leads


def damage_missing_source(files):
    del files["cordoba"]


def damage_unknown_status(files):
    edit(files["citizen"], lambda t: t.replace(",Enrolled,", ",Brand New Status,", 1))


def damage_closed_tab(files):
    edit(files["debtblue:closed"], lambda t: "\n".join(t.splitlines()[:-1]) + "\n")


def damage_too_few_rows(files):
    edit(files["ringcx"], lambda t: "\n".join(t.splitlines()[:100]) + "\n")      # below min_rows for RingCX


@pytest.mark.parametrize("damage,expect", [
    (damage_few_paid_leads, "enough paid leads"),
    (damage_missing_source, "all sources readable"),
    (damage_unknown_status, "no unexpected lender statuses"),
    (damage_closed_tab, "match the lender's Closed tab"),
    (damage_too_few_rows, "ringcx: at least"),
], ids=lambda x: x if isinstance(x, str) else x.__name__)
def test_failing_runs_fail_closed_and_report_identically(tmp_path, monkeypatch, good_files, damage, expect):
    files = copy_files(good_files, tmp_path / "damaged")
    damage(files)
    rc_f, text_f, pub_f = run_build(tmp_path, monkeypatch, files, "full", "--ingest", "full")
    rc_s, text_s, pub_s = run_build(tmp_path, monkeypatch, files, "staged", "--ingest", "staged")
    assert rc_f == rc_s == 1 and pub_f is None and pub_s is None        # nothing published by either flow
    assert expect in text_f and expect in text_s
    assert checks_of(text_f) == checks_of(text_s)


def test_a_failed_read_is_never_treated_as_an_empty_source(tmp_path, monkeypatch, good_files):
    files = copy_files(good_files, tmp_path / "gone")
    Path(files["cordoba"]).unlink()                                    # listed in the map but unreadable
    reports = {}
    for flag in ("full", "staged"):
        rc, text, pub = run_build(tmp_path, monkeypatch, files, flag, "--ingest", flag)
        assert rc == 1 and pub is None and "**Result:** FAILED" in text       # fails closed; nothing published
        reports[flag] = checks_of(text)
    assert reports["full"] == reports["staged"]


# ---------- fallbacks ----------

def test_cases_that_need_the_full_tables_use_the_default_flow():
    assert staged_mod.unavailable_reason(closer_on=True, uses_database=False)
    assert staged_mod.unavailable_reason(closer_on=False, uses_database=True)
    assert staged_mod.unavailable_reason(closer_on=False, uses_database=False) is None


def test_closing_agent_preview_falls_back_and_says_so(tmp_path, monkeypatch, good_files):
    def boom(*a, **k):
        raise AssertionError("staged must not run with the closing-agent preview")

    monkeypatch.setattr(staged_mod, "ingest", boom)
    rc, text, _ = run_build(tmp_path, monkeypatch, good_files, "closer", "--ingest", "staged", "--closer-preview")
    assert "- staged ingestion: not used: the closing-agent preview needs the full lender tables" in text


# ---------- the reduced tables equal the default flow's, directly ----------

class FakeSource:
    """Serves prebuilt tables the way GoogleSheetsSource.read does, and remembers the order of the reads."""

    def __init__(self, cfg, tables):
        self.by_sheet = {s["worksheet"]: k for k, s in cfg["sources"].items()}      # data tabs have distinct names
        self.by_id = {s["spreadsheet_id"]: k for k, s in cfg["sources"].items()}    # every lender's Closed tab is just "Closed"
        self.tables, self.reads, self.cfg = tables, [], cfg

    def read(self, sid, worksheet, cols, whole_tab=False):
        key = self.by_sheet[worksheet]
        self.reads.append(key)
        return self.tables[key]

    def count_rows(self, sid, worksheet, has_header):
        return int(rules.closed_status_rows(self.tables, self.cfg)[self.by_id[sid]])


@pytest.fixture(scope="module")
def tables(cfg):
    return generate(20_000, seed=5, cfg=cfg)


def test_staged_tables_equal_the_default_flow_tables(cfg, tables):
    src = FakeSource(cfg, tables)
    st = staged_mod.ingest(cfg, src)
    ringba = rules.prepare_ringba(tables["ringba"], cfg)
    leads, diag = rules.select_paid_leads(ringba, cfg)
    phones = set(leads["phone"])
    assert src.reads == ["ringba", "ringcx", "citizen", "cordoba", "debtblue"]       # Ringba first, then config order
    pd.testing.assert_frame_equal(st.leads, leads, check_exact=True)
    assert st.diag == diag and st.rule_error is None
    assert st.gaps == rules.months_without_calls(ringba, leads["call_time"].min(), leads["call_time"].max())
    full_rcx = rules.prepare_ringcx(tables["ringcx"], cfg)
    pd.testing.assert_frame_equal(st.ringcx, full_rcx[full_rcx["phone"].isin(phones)], check_exact=True)
    pd.testing.assert_frame_equal(st.crm, rules.prepare_lenders(tables, cfg, phones=leads["phone"]), check_exact=True)
    assert st.sizes == {k: len(v) for k, v in tables.items()}
    assert st.unknown == rules.unknown_statuses(tables, cfg)
    assert st.closed_rows == rules.closed_status_rows(tables, cfg)
    assert st.closed_tab_rows == rules.closed_status_rows(tables, cfg)               # what the fake Closed tab reports


def test_lender_pieces_join_to_the_full_table_even_when_some_lenders_have_no_match(cfg, tables):
    full = rules.prepare_lenders(tables, cfg)
    for key in cfg["lenders"]["order"]:                                              # phones present in exactly one lender
        others = {x for k in cfg["lenders"]["order"] if k != key
                  for x in rules.prepare_lenders(tables, cfg, only=(k,))["phone"]}
        only_here = sorted(set(full[full["lender"] == key]["phone"]) - others)[:25]
        assert only_here
        want = rules.prepare_lenders(tables, cfg, phones=only_here)
        pieces = [rules.prepare_lenders(tables, cfg, phones=only_here, only=(k,)) for k in cfg["lenders"]["order"]]
        assert sum(len(p) == 0 for p in pieces) >= 1                                  # at least one empty piece
        pd.testing.assert_frame_equal(staged_mod._join(pieces), want, check_exact=True)
    assert len(staged_mod._join([rules.prepare_lenders(tables, cfg, phones=["5559999999"], only=(k,))
                                 for k in cfg["lenders"]["order"]])) == 0           # nothing matches anywhere


def test_only_defaults_to_every_lender(cfg, tables):
    assert rules.unknown_statuses(tables, cfg, only=None) == rules.unknown_statuses(tables, cfg)
    assert set(rules.closed_status_rows(tables, cfg, only=("cordoba",))) == {"cordoba"}
    pd.testing.assert_frame_equal(rules.prepare_lenders(tables, cfg, only=None), rules.prepare_lenders(tables, cfg), check_exact=True)


def test_too_few_paid_leads_still_reads_every_source_and_reduces_none(cfg, tables):
    c = copy.deepcopy(cfg)
    c["report"]["window_size"] = 10_000_000                                          # can never be met
    src = FakeSource(c, tables)
    st = staged_mod.ingest(c, src)
    assert st.rule_error and "unique paid leads" in st.rule_error
    assert src.reads == ["ringba", "ringcx", "citizen", "cordoba", "debtblue"]       # same reads, so same checks as the default flow
    assert st.leads is None and st.ringcx is None and st.crm is None
    assert st.sizes == {k: len(v) for k, v in tables.items()} and st.closed_rows


def test_a_read_error_propagates_and_is_timed_by_name(cfg, tables):
    class Failing(FakeSource):
        def read(self, sid, worksheet, cols, whole_tab=False):
            if self.by_sheet[worksheet] == "cordoba":
                raise SourceError("cordoba: Google Sheets still unavailable")
            return super().read(sid, worksheet, cols, whole_tab)

    timings: dict = {}
    with pytest.raises(SourceError):
        staged_mod.ingest(cfg, Failing(cfg, tables), timings=timings)
    assert list(timings) == ["ringba", "ringcx", "citizen", "citizen closed tab", "cordoba"] and timings["cordoba"]["rows"] is None


def test_read_source_is_what_read_all_uses(cfg, tables):
    src = FakeSource(cfg, tables)
    all_tables, closed = read_all(cfg, src)
    assert set(all_tables) == set(tables) and set(closed) == {"citizen", "cordoba", "debtblue"}
