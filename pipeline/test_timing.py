"""Per-source fetch timing and per-stage timing in the validation report (made-up data only)."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pandas as pd
import pytest

from benchmarks.synth import generate
from pipeline import build
from pipeline.config import load_config
from pipeline.sheets import LocalSource, SourceError, read_all
from pipeline.timing import StageTimer, peak_memory_mb

CFG_PATH = Path(__file__).resolve().parent.parent / "config" / "report.yaml"


class Ticker:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_laps_credit_the_time_since_the_previous_lap():
    clock = Ticker()
    timer = StageTimer(clock)
    clock.t = 2.0
    timer.lap("a")
    clock.t = 2.5
    timer.lap("b")
    clock.t = 5.5
    timer.lap("a")
    assert timer.seconds == {"a": 5.0, "b": 0.5}
    assert list(timer.seconds) == ["a", "b"]


def test_report_lines_name_each_source_and_hold_numbers_only():
    timer = StageTimer(Ticker())
    timer.lap("read sources")
    fetch = {"ringba": {"seconds": 12.34, "rows": 150000, "retries": 0},
             "ringcx": {"seconds": 190.0, "rows": 40000, "retries": 1},
             "citizen closed tab": {"seconds": 3.0, "rows": 9000, "retries": 2}}
    facts = timer.facts(fetch, 512.4)
    assert facts["fetch ringba"] == "12.3 s, 150000 rows, 0 retries"
    assert facts["fetch ringcx"] == "190.0 s, 40000 rows, 1 retry"
    assert facts["fetch citizen closed tab"] == "3.0 s, 9000 rows, 2 retries"
    assert facts["fetch total"] == "205.3 s"
    assert facts["stage read sources"] == "0.00 s" and facts["peak memory"] == "512 MB"


def test_peak_memory_is_reported_on_this_platform():
    assert peak_memory_mb() > 10


def csv_cells(df: pd.DataFrame, time_cols: list[str], phone_cols: list[str]) -> pd.DataFrame:
    """Text a spreadsheet export would hold: readable timestamps instead of serial numbers, whole phones."""
    from pipeline.rules import EXCEL_EPOCH

    d = df.drop(columns="_row").astype(object)
    for c in time_cols:
        d[c] = [(EXCEL_EPOCH + pd.to_timedelta(v, unit="D")).round("s").strftime("%Y-%m-%d %H:%M:%S")
                if isinstance(v, float) and v == v else ("" if v is None else v) for v in d[c]]
    for c in phone_cols:
        d[c] = [int(v) if isinstance(v, float) else v for v in d[c]]
    return d


def local_files(tmp_path: Path, cfg: dict) -> dict:
    """Write synthetic sources to CSV in the layout LocalSource reads (header row, then data), plus one
    Closed-tab file per lender holding exactly its closed-status rows (no header, as configured for citizen/cordoba)."""
    t = generate(20_000, seed=3, cfg=cfg)
    src, files = cfg["sources"], {}
    for key, df in t.items():
        c = src[key]["columns"]
        if key == "ringba":
            times, phones = [c["call_time"]], [c["phone"]]
        elif key == "ringcx":
            times, phones = [c["date"]], [c["phone"]]
        else:
            times, phones = [c["close_date"]], c["phones"]
        d = csv_cells(df, times, phones)
        p = tmp_path / f"{key}.csv"
        d.to_csv(p, index=False, header=True)
        files[key] = str(p)
        if key in cfg["lenders"]["order"]:
            ct = src[key]["closed_tab"]
            closed = d[d[c["status"]].isin(cfg["lenders"]["closed_statuses"][key])]
            pc = tmp_path / f"{key}_closed.csv"
            closed.to_csv(pc, index=False, header=ct["has_header"])
            files[key + ":closed"] = str(pc)
    return files


def test_read_all_records_every_read_in_order_with_rows():
    cfg = load_config(CFG_PATH)
    t = generate(20_000, seed=3, cfg=cfg)

    class Fake:
        retries = 0

        def start_source(self):
            pass

        def read(self, sid, ws, cols, whole_tab=False):
            self.retries += 1 if ws == "Two Minute Transfers" else 0
            key = next(k for k, s in cfg["sources"].items() if s["worksheet"] == ws)
            return t[key]

        def count_rows(self, sid, ws, has_header):
            return 123

    timings: dict = {}
    tables, closed = read_all(cfg, Fake(), timings=timings)
    assert list(timings) == ["ringba", "ringcx", "citizen", "citizen closed tab", "cordoba", "cordoba closed tab",
                             "debtblue", "debtblue closed tab"]
    assert timings["ringba"]["rows"] == len(tables["ringba"]) == 20_000
    assert timings["ringcx"]["retries"] == 1 and timings["ringba"]["retries"] == 0
    assert timings["citizen closed tab"]["rows"] == 123
    assert all(v["seconds"] >= 0 for v in timings.values())


def test_a_failed_read_is_still_timed_and_still_raises():
    cfg = load_config(CFG_PATH)

    class Fake:
        def read(self, sid, ws, cols, whole_tab=False):
            if ws == "Two Minute Transfers":
                raise SourceError("boom")
            return pd.DataFrame({c: [] for c in cols} | {"_row": []})

    timings: dict = {}
    with pytest.raises(SourceError):
        read_all(cfg, Fake(), timings=timings)
    assert list(timings) == ["ringba", "ringcx"] and timings["ringcx"]["rows"] is None


def test_read_all_without_timings_behaves_as_before(tmp_path):
    cfg = copy.deepcopy(load_config(CFG_PATH))
    tables, closed = read_all(cfg, LocalSource(local_files(tmp_path, cfg)))
    assert set(tables) == {"ringba", "ringcx", "citizen", "cordoba", "debtblue"} and set(closed) == {"citizen", "cordoba", "debtblue"}


def test_retries_are_counted_by_the_google_source():
    from pipeline.test_rules import Resp, make_source, rcx_body, RCX_ROWS, unavailable

    src, _, _ = make_source([unavailable(), unavailable(), Resp(200, rcx_body(RCX_ROWS))])
    assert src.retries == 0
    src.read("1" * 30, "Two Minute Transfers", ["Date", "ANI"], whole_tab=True)
    assert src.retries == 2


def run_build(tmp_path: Path, monkeypatch, files: dict) -> tuple[int, str]:
    monkeypatch.setattr(build, "fetch_baseline", lambda url: None)      # no network in tests
    (tmp_path / "map.json").write_text(json.dumps(files))
    out = tmp_path / "out"
    rc = build.main(["--out", str(out), "--local-files", str(tmp_path / "map.json"), "--preview"])
    return rc, (out / "validation.md").read_text(encoding="utf-8")


def test_full_build_report_carries_every_timing_and_no_data(tmp_path, monkeypatch):
    rc, text = run_build(tmp_path, monkeypatch, local_files(tmp_path, load_config(CFG_PATH)))
    assert rc == 0 and "**Result:** PASSED" in text
    for name in ("fetch ringba", "fetch ringcx", "fetch citizen", "fetch citizen closed tab", "fetch debtblue closed tab",
                 "fetch total", "stage read sources", "stage validate inputs", "stage parse ringba, select paid leads",
                 "stage parse ringcx, match agents", "stage parse lenders, match closes", "stage closing agent",
                 "stage validate business rules", "stage build public rows", "stage fetch published baseline",
                 "stage final checks", "stage finish", "peak memory"):
        assert f"- {name}:" in text, name
    assert "5550" not in text            # no synthetic phone digits reached the report


def test_a_failed_source_read_still_reports_which_source_and_how_long(tmp_path, monkeypatch):
    files = local_files(tmp_path, load_config(CFG_PATH))
    del files["cordoba"]                 # not readable: the run must fail closed, not treat it as empty
    rc, text = run_build(tmp_path, monkeypatch, files)
    assert rc == 1 and "**Result:** FAILED" in text and "all sources readable" in text
    assert "- fetch ringba:" in text and "- fetch citizen closed tab:" in text and "- fetch cordoba: " in text
    assert "- stage finish:" in text and "- fetch debtblue:" not in text      # nothing after the failure was read


def test_a_run_that_fails_a_rule_reports_the_stage_it_stopped_in(tmp_path, monkeypatch):
    files = local_files(tmp_path, load_config(CFG_PATH))
    p = Path(files["ringba"])
    p.write_text("\n".join(p.read_text().splitlines()[:1100]) + "\n")          # 1,099 calls: enough rows, too few paid leads
    rc, text = run_build(tmp_path, monkeypatch, files)
    assert rc == 1 and "enough paid leads" in text
    assert "- stage validate inputs:" in text and "- stage finish:" in text and "- stage parse ringcx, match agents:" not in text
