"""prepare_lenders(phones=...) must change nothing the report can see (made-up data only).

The filtered table is held to three references: the full table built by the same code, the original
cell-by-cell implementation (ref_prepare_lenders), and, for the report itself, the output of match_closes and of
a whole build. Edge cases are written out by hand and then explored with seeded random tables built to collide:
the same number written several ways, in several lenders and in two phone columns, with odd statuses and dates.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from benchmarks.synth import generate
from pipeline import build, rules
from pipeline.config import load_config
from pipeline.test_timing import local_files, run_build
from pipeline.test_vectorized import ref_prepare_lenders

CFG_PATH = Path(__file__).resolve().parent.parent / "config" / "report.yaml"
EPOCH = pd.Timestamp("1899-12-30")


@pytest.fixture(scope="module")
def cfg():
    return load_config(CFG_PATH)


def serial(day: str, hours: float = 0.0) -> float:
    return float((pd.Timestamp(day) - EPOCH).days) + hours / 24


def lender_tables(cfg: dict, rows: dict[str, list[dict]]) -> dict[str, pd.DataFrame]:
    """Source tables in the shape the Sheets reads give: one dict per record with phones / status / close / debt / agent."""
    out = {}
    for key in cfg["lenders"]["order"]:
        c, recs = cfg["sources"][key]["columns"], rows.get(key, [])
        data = {col: [r["phones"][i] if i < len(r["phones"]) else None for r in recs] for i, col in enumerate(c["phones"])}
        data[c["status"]] = [r.get("status") for r in recs]
        data[c["close_date"]] = [r.get("close") for r in recs]
        if c["debt"]:
            data[c["debt"]] = [r.get("debt") for r in recs]
        data[c["agent"]] = [r.get("agent") for r in recs]
        df = pd.DataFrame(data, dtype=object)
        df["_row"] = range(2, len(recs) + 2)
        out[key] = df
    return out


def leads_frame(phones, call_times) -> pd.DataFrame:
    return pd.DataFrame({"phone": list(phones), "call_time": pd.to_datetime(list(call_times))})


def assert_same_closes(cfg, leads, tables, wanted=None, original_dtypes=True):
    """match_closes gives identical leads and excluded tables from the full, the filtered and the original table.

    `original_dtypes=False` compares the original implementation by value only: with a source that has no rows at
    all, pandas typed its empty columns differently (object / float64 flags) from the column parsers. Such a source
    cannot reach a real run (every source must have min_rows rows), and the filtered table still matches the full one exactly.
    """
    wanted = list(leads["phone"]) if wanted is None else wanted
    full = rules.match_closes(leads, rules.prepare_lenders(tables, cfg), cfg)
    filt = rules.match_closes(leads, rules.prepare_lenders(tables, cfg, phones=wanted), cfg)
    orig = rules.match_closes(leads, ref_prepare_lenders(tables, cfg), cfg)
    for got, strict in ((filt, True), (orig, original_dtypes)):
        pd.testing.assert_frame_equal(got[0], full[0], check_exact=True, check_dtype=strict)
        pd.testing.assert_frame_equal(got[1], full[1], check_exact=True, check_dtype=strict)
    return filt


def assert_table_is_the_restriction(cfg, tables, wanted):
    """The filtered table is exactly the original full table restricted to the wanted phones (fresh index)."""
    got = rules.prepare_lenders(tables, cfg, phones=wanted)
    ref = ref_prepare_lenders(tables, cfg)
    keep = {x for x in wanted if isinstance(x, str) and x}
    pd.testing.assert_frame_equal(got, ref[ref["phone"].isin(keep)].reset_index(drop=True), check_exact=True)
    same_build = rules.prepare_lenders(tables, cfg)
    pd.testing.assert_frame_equal(got, same_build[same_build["phone"].isin(keep)].reset_index(drop=True), check_exact=True)


# ---------- the table itself, on realistic volumes ----------

def test_filtered_table_is_the_original_table_restricted_to_the_wanted_phones(cfg):
    t = generate(20_000, seed=5, cfg=cfg)
    ringba = rules.prepare_ringba(t["ringba"], cfg)
    leads, _ = rules.select_paid_leads(ringba, cfg)
    full = rules.prepare_lenders(t, cfg)
    rng = np.random.default_rng(1)
    everyone = full["phone"].unique()
    cases = {
        "the selected leads": list(leads["phone"]),
        "a lead series, as build passes it": leads["phone"],
        "half of all lender phones": list(rng.choice(everyone, len(everyone) // 2, replace=False)),
        "every phone": list(everyone),
        "none": [],
        "numbers in no lender": ["5559999999", "5558888888"],
        "blanks and non-text ignored": ["", None, np.nan, 5550000001, *leads["phone"][:5]],
        "duplicates in the request": list(leads["phone"]) * 3,
    }
    for name, wanted in cases.items():
        try:
            assert_table_is_the_restriction(cfg, t, wanted)
        except AssertionError as e:
            raise AssertionError(f"{name}: {e}") from None
    assert len(rules.prepare_lenders(t, cfg, phones=leads["phone"])) < len(full) / 50      # and it really is much smaller


def test_filtered_table_has_the_same_columns_and_types(cfg):
    t = generate(20_000, seed=5, cfg=cfg)
    full = rules.prepare_lenders(t, cfg)
    for wanted in (list(full["phone"][:50]), []):
        got = rules.prepare_lenders(t, cfg, phones=wanted)
        assert list(got.columns) == list(full.columns) and (got.dtypes == full.dtypes).all()
        assert list(got.index) == list(range(len(got)))


# ---------- hand-written edge cases, end to end through match_closes ----------

def scenario(cfg):
    """One table with every case the matching rules distinguish, and the leads that probe them."""
    rows = {
        "citizen": [
            {"phones": ["5550000001"], "status": "Enrolled", "close": serial("2026-05-10"), "debt": 11000.0, "agent": "A"},       # L1 later than DebtBlue
            {"phones": ["5550000002"], "status": "Enrolled", "close": serial("2026-05-10"), "debt": 12000.0, "agent": "A"},       # L2 tie with Cordoba: Citizen first
            {"phones": ["+1 555 000 0005"], "status": "Pending", "close": serial("2026-05-12"), "debt": 1.0},                     # L5 non-closing status
            {"phones": ["5550000007"], "status": "Enrolled", "close": None, "debt": 5.0},                                         # L7 closed status, no date
            {"phones": ["5550000008"], "status": "Enrolled", "close": serial("2026-04-01"), "debt": 7.0},                          # L8 closes before the paid call
            {"phones": ["5550000009"], "status": "Enrolled", "close": serial("2026-05-15"), "debt": 100.0, "agent": "first"},     # L9 two rows, same date
            {"phones": ["5550000009"], "status": " Enrolled ", "close": serial("2026-05-15"), "debt": 200.0, "agent": "second"},
            {"phones": ["5550000013"], "status": "Enrolled", "close": serial("2026-05-11", 13.5), "debt": "$1,234.50"},           # L13 text debt, mid-day
            {"phones": ["5550000014"], "status": "Enrolled", "close": "2026-05-11 08:00:00", "debt": 3.0},                          # L14 text date
            {"phones": ["555-0006"], "status": "Enrolled", "close": serial("2026-05-10"), "debt": 9.0},                            # short number: unusable
            {"phones": [None], "status": "Enrolled", "close": serial("2026-05-10")},                                              # blank phone
            {"phones": ["5551110000"], "status": "Enrolled", "close": serial("2026-05-10"), "debt": 1.0},                          # not a lead
        ],
        "cordoba": [
            {"phones": ["5550000001", None], "status": "Active", "close": serial("2026-05-10"), "debt": 21000.0},                  # L1 (home)
            {"phones": [None, "5550000002"], "status": "Active", "close": serial("2026-05-10"), "debt": 22000.0},                  # L2 (cell only)
            {"phones": ["5550000003", "(555) 000-0003"], "status": "Active", "close": serial("2026-05-12"), "debt": 3.0},         # L3 same number twice
            {"phones": [None, "5550000004"], "status": "Waiting For First Payment", "close": serial("2026-05-12"), "debt": 4.0},  # L4 second column only
            {"phones": [15550000005, 5550000005.0], "status": "Cancelled", "close": serial("2026-05-20"), "debt": 5.0},            # L5 closed but inactive
            {"phones": ["5550000008", None], "status": "Active", "close": serial("2026-05-30"), "debt": 8.0},                      # L8 later record qualifies
            {"phones": ["5550000010", "5550000011"], "status": "Active", "close": serial("2026-05-09"), "debt": 1000.0},           # L10 and L11 from one row
            {"phones": ["5550000012", "5550000010"], "status": "Active", "close": serial("2026-05-08"), "debt": 50.0},             # L10 again, earlier
            {"phones": ["5550000015", None], "status": "Returned", "close": serial("2026-05-09"), "debt": 15.0},                   # L15 returned
            {"phones": ["5550000016", None], "status": "Surprise", "close": serial("2026-05-09"), "debt": 16.0},                   # L16 unknown status
            {"phones": ["5550000017", None], "status": None, "close": serial("2026-05-09"), "debt": None},                         # L17 blank status
        ],
        "debtblue": [
            {"phones": ["5550000001"], "status": "Enrolled", "close": serial("2026-05-01"), "agent": "B"},                         # L1 earliest wins
            {"phones": [5550000005.0], "status": "Contacted", "close": serial("2026-05-02")},                                      # L5 non-closing
            {"phones": ["5550000012"], "status": "Enrolled", "close": serial("2026-05-09")},                                       # not a lead
            {"phones": ["5550000018"], "status": "Dropped / Cancelled", "close": serial("2026-05-06")},                            # L18 closed inactive
        ],
    }
    ids = list(range(1, 19))
    phones = [f"55500{i:05d}" for i in ids]
    calls = ["2026-04-28" if i == 1 else "2026-05-05" for i in ids]
    return lender_tables(cfg, rows), leads_frame(phones, calls)


def test_edge_cases_give_identical_results_and_the_expected_ones(cfg):
    tables, leads = scenario(cfg)
    out, excluded = assert_same_closes(cfg, leads, tables)
    r = out.set_index("phone")
    p = lambda i: f"55500{i:05d}"  # noqa: E731
    assert r.loc[p(1), "close_lender_key"] == "debtblue"                                  # earliest close beats lender order
    assert r.loc[p(2), "close_lender_key"] == "citizen"                                   # same date: Citizen, then Cordoba
    assert r.loc[p(3), "close_lender_key"] == "cordoba" and r.loc[p(4), "close_lender_key"] == "cordoba"
    assert r.loc[p(5), "closed"] and not r.loc[p(5), "active"] and r.loc[p(5), "close_status"] == "Cancelled"
    assert not r.loc[p(6), "in_lender_crm"]                                               # short number is blank
    assert not r.loc[p(7), "closed"] and r.loc[p(7), "in_citizen"]                        # closed status but no date
    assert r.loc[p(8), "close_lender_key"] == "cordoba"                                   # the earlier Citizen record is before the paid call
    assert r.loc[p(9), "close_row"] == 7 and r.loc[p(9), "debt_enrolled"] == 100.0       # same date: first sheet row
    assert r.loc[p(10), "close_row"] == 9 and r.loc[p(11), "close_row"] == 8             # one row serves two leads; earlier record wins
    assert r.loc[p(13), "debt_enrolled"] == 1234.5 and r.loc[p(14), "closed"]
    assert not r.loc[p(15), "active"] and r.loc[p(15), "closed"]                          # Returned: closed, not active
    assert not r.loc[p(16), "closed"] and not r.loc[p(17), "closed"]
    assert r.loc[p(18), "close_lender_key"] == "debtblue" and not r.loc[p(18), "active"]
    assert excluded["phone"].nunique() == 2                                               # the dateless and the pre-call record


@pytest.mark.parametrize("subset", [[1], [1, 2], [5], [7, 8], [9], [10, 11], [3, 4, 6], [16, 17], list(range(1, 19))])
def test_edge_cases_hold_for_any_subset_of_leads(cfg, subset):
    tables, leads = scenario(cfg)
    chosen = leads[leads["phone"].isin([f"55500{i:05d}" for i in subset])].reset_index(drop=True)
    assert_same_closes(cfg, chosen, tables)


def test_a_lead_nobody_holds_and_empty_requests(cfg):
    tables, leads = scenario(cfg)
    nobody = leads_frame(["5557777777"], ["2026-05-05"])
    out, excluded = assert_same_closes(cfg, nobody, tables)
    assert not out["closed"].any() and not out["in_lender_crm"].any() and excluded.empty
    empty = rules.prepare_lenders(tables, cfg, phones=[])
    assert empty.empty and set(empty.columns) >= {"phone", "status", "close_date", "is_closed", "is_active"}
    assert_same_closes(cfg, leads.iloc[:0], tables)


def test_sources_with_no_rows(cfg):
    tables = lender_tables(cfg, {})
    out, excluded = assert_same_closes(cfg, leads_frame(["5550000001"], ["2026-05-05"]), tables, original_dtypes=False)
    assert not out["closed"].any()


def test_a_value_that_cannot_be_read_still_stops_the_run_wherever_it_sits(cfg):
    """Close dates are parsed over every row, so one impossible serial number fails the build even in a row the filter drops."""
    rows = {"citizen": [{"phones": ["5550000001"], "status": "Enrolled", "close": serial("2026-05-10")},
                        {"phones": ["5559999999"], "status": "Enrolled", "close": 150000.0}]}
    tables = lender_tables(cfg, rows)
    with pytest.raises(ValueError):
        rules.prepare_lenders(tables, cfg)
    with pytest.raises(ValueError):
        rules.prepare_lenders(tables, cfg, phones=["5550000001"])


# ---------- random tables built to collide ----------

STATUSES = (["Enrolled", "Submitted", "Active", "Cancelled", "Returned", "Waiting For First Payment", "Contact", "Dropped / Cancelled",
             "Pending", "Contacted", "Rejected", "Surprise", " Enrolled ", "", None])


def random_scenario(cfg: dict, rng: np.random.Generator, n: int = 60, pool: int = 14):
    def raw(i: int):
        v = 5550000000 + i
        forms = [str(v), f"+1{v}", f"({str(v)[:3]}) {str(v)[3:6]}-{str(v)[6:]}", v, float(v), f" {v} ", f"1-{str(v)[:3]}-{str(v)[3:6]}-{str(v)[6:]}"]
        return forms[rng.integers(len(forms))]

    def phone_cell():
        r = rng.random()
        return None if r < 0.08 else ("" if r < 0.12 else ("12345" if r < 0.16 else raw(int(rng.integers(pool)))))

    def close_cell():
        r = rng.random()
        if r < 0.55:
            return serial("2026-05-01") + float(rng.integers(0, 50)) + (float(rng.integers(0, 24)) / 24 if rng.random() < 0.5 else 0.0)
        if r < 0.7:
            return int(serial("2026-05-01") + rng.integers(0, 50))
        if r < 0.85:
            return f"2026-{rng.integers(5, 7):02d}-{rng.integers(1, 29):02d} {rng.integers(0, 24):02d}:00:00"
        return [None, "", "not a date", True][rng.integers(4)]

    rows: dict[str, list[dict]] = {}
    for key in cfg["lenders"]["order"]:
        width = len(cfg["sources"][key]["columns"]["phones"])
        rows[key] = [{"phones": [phone_cell() for _ in range(width)], "status": STATUSES[rng.integers(len(STATUSES))],
                      "close": close_cell(), "debt": [float(rng.integers(0, 90000)), None, "$1,000.50", "", "abc", 0][rng.integers(6)],
                      "agent": [None, "Ana", " Ben "][rng.integers(3)]} for _ in range(n)]
    wanted = sorted({f"{5550000000 + int(i)}" for i in rng.choice(pool + 4, size=int(rng.integers(1, pool)), replace=False)})
    calls = [f"2026-{rng.integers(4, 7):02d}-{rng.integers(1, 29):02d} {rng.integers(0, 24):02d}:{rng.integers(0, 60):02d}" for _ in wanted]
    return lender_tables(cfg, rows), leads_frame(wanted, calls)


@pytest.mark.parametrize("seed", range(150))
def test_random_colliding_tables_give_identical_results(cfg, seed):
    tables, leads = random_scenario(cfg, np.random.default_rng(seed))
    assert_table_is_the_restriction(cfg, tables, list(leads["phone"]))
    assert_same_closes(cfg, leads, tables)


# ---------- the whole build ----------

def test_the_build_asks_for_exactly_the_selected_leads_phones(tmp_path, monkeypatch, cfg):
    seen = {}
    real = rules.prepare_lenders

    def spy(tables, c, phones=None):
        seen["phones"] = None if phones is None else sorted(phones)
        return real(tables, c, phones=phones)

    monkeypatch.setattr(build.rules, "prepare_lenders", spy)
    rc, _ = run_build(tmp_path, monkeypatch, local_files(tmp_path, cfg))
    data = json.loads((tmp_path / "out" / "site" / "data.json").read_text())
    assert rc == 0 and len(seen["phones"]) == 300 == len(data["rows"])


def test_published_files_are_identical_with_and_without_the_filter(tmp_path, monkeypatch, cfg):
    files = local_files(tmp_path, cfg)
    published = {}
    for mode in ("filtered", "full"):
        work = tmp_path / mode
        work.mkdir()
        if mode == "full":
            real = rules.prepare_lenders
            monkeypatch.setattr(build.rules, "prepare_lenders", lambda tables, c, phones=None: real(tables, c))
        rc, _ = run_build(work, monkeypatch, files)
        assert rc == 0
        site = work / "out" / "site"
        data, summary = json.loads((site / "data.json").read_text()), json.loads((site / "summary.json").read_text())
        data.pop("refreshed_at"), summary.pop("refreshed_at")
        published[mode] = (data, summary, (site / "index.html").read_bytes())
    assert published["filtered"] == published["full"]
    assert published["filtered"][1]["totals"]["closed"] > 0          # the comparison covered real closes
