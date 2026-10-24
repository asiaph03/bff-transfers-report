"""The column parsers must give exactly what the scalar parsers give, value for value (made-up data only).

The scalar functions in rules.py define the rules; the *_series functions are the fast versions. These tests
hold the two together on hand-picked edge cases, on randomised mixed columns, and on whole prepare_* tables
against the original cell-by-cell implementations (copied below as the reference).
"""
from __future__ import annotations

import datetime as dt
import decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from benchmarks.synth import generate
from pipeline import rules
from pipeline.config import load_config

CFG_PATH = Path(__file__).resolve().parent.parent / "config" / "report.yaml"
NAN = float("nan")


def same(fast: pd.Series, ref: pd.Series) -> None:
    pd.testing.assert_series_equal(fast.reset_index(drop=True), ref.reset_index(drop=True),
                                   check_names=False, check_exact=True)


def ref_time(col: pd.Series) -> pd.Series:
    return pd.to_datetime(col.map(rules.parse_time))


def ref_phone(col: pd.Series, digits=10, min_digits=10) -> pd.Series:
    return col.map(lambda v: rules.norm_phone(v, digits, min_digits))


TIME_VALUES = [
    None, "", "   ", NAN, 0, 0.0, 0.5, 1, 1.0, 1.0000000001, 2.5, 46000, 46000.5, 46000.69013889, 45999.999999999,
    106751.5, 200000, 1e9, -5, float("inf"), -float("inf"), True, False, np.int64(46000), np.float32(46000.5),
    np.float64(46000.25), "2026-03-04", "2026-03-04 10:11:12", "2026-03-04T10:11", " 2026-03-04 ",
    "2026-03-04 10:11:12.123456789", "2026-03-04 23:59:59.9999995", "2026-02-30", "2026-13-01", "0001-01-01",
    "3000-01-01", "1677-09-21", "2262-04-12", "03/04/2026", "March 4, 2026 3:15 PM", "not a date", "46000.5", "nan", "NaT",
]
# Values the fast path hands to the scalar parser. Time-zone-aware text cannot share a column with naive
# times (the original code raises on that too), so each of these is tested alone.
TIME_SCALAR_ONLY = [dt.datetime(2026, 3, 4, 10, 11), pd.Timestamp("2026-03-04 10:11"), dt.date(2026, 3, 4), pd.NaT,
                    "2026-03-04T10:11:12Z", "2026-03-04 10:11:12+02:00"]
PHONE_VALUES = [
    None, NAN, "", " ", "5551234567", "+1 (555) 123-4567", "1-555-123-4567", "555-1234", "12345678901234", "abc",
    " 555 123 4567 ", "٥٥٥١٢٣٤٥٦٧", "５５５１２３４５６７", 5551234567, 15551234567, 0, -5551234567, 10**30, 2**63, 2**63 - 1,
    5551234567.0, 15551234567.0, -0.0, 5551234567.5, 1.5551234567e10, 1e22, float("inf"), -float("inf"), True, False,
    np.int64(5551234567), np.uint64(2**63 + 5), np.float64(5551234567.0), np.float32(1.5), decimal.Decimal("5551234567"),
    b"5551234567", pd.NA,
]
TEXT_VALUES = [None, NAN, "", " ", " a ", "x\n", "\tTabbed\t", "ünï ", 5, 5.0, 5.5, -3, True, np.int64(3), np.float64(2.5),
               decimal.Decimal("1.5"), pd.NA, "Inbound Accepted", 10**30, float("inf")]
NUMBER_VALUES = [None, NAN, "", "  ", "$1,234.50", "1234", "-5", "-5.25", "+5", "5.", ".5", "1e5", "inf", "nan", "1_000",
                 "$", "12abc", " 12 ", "１２", "1 234.5", "0", "-0", "007", 5, 5.5, True, False, np.float32(1.5), np.int64(7),
                 np.float64(2.25), decimal.Decimal("2.5"), 10**20, float("inf")]
BOOL_VALUES = [True, False, np.bool_(True), np.bool_(False), "TRUE", "true", " True ", "tRuE", "FALSE", "yes", "1", "", 1, 0,
               None, NAN, 1.0, 0.5, "TRUE ", "\tTRUE", decimal.Decimal(1), pd.NA, "T", "TRU", "TRUEISH", "TRUE1", "NOT TRUE", "TRUE TRUE"]


def obj(values) -> pd.Series:
    return pd.Series(list(values), dtype=object)


# ---------- single values and whole columns ----------

@pytest.mark.parametrize("v", TIME_VALUES + TIME_SCALAR_ONLY, ids=repr)
def test_time_each_value_alone(v):
    col = obj([v])
    same(rules.parse_time_series(col), ref_time(col))


def test_time_all_values_in_one_column_on_the_fast_path():
    col = obj(TIME_VALUES)
    assert rules._parse_time_fast(col) is not None          # this mix really takes the fast path
    same(rules.parse_time_series(col), ref_time(col))


def test_time_column_with_values_for_the_scalar_parser():
    col = obj(TIME_VALUES + TIME_SCALAR_ONLY[:4])
    assert rules._parse_time_fast(col) is None
    same(rules.parse_time_series(col), ref_time(col))


def test_time_fast_path_hands_over_timezone_aware_text():
    col = obj(["2026-03-04T10:11:12Z", "2026-03-04T10:11:12Z", "2026-03-05T10:11:12Z"])
    assert rules._parse_time_fast(col) is None
    same(rules.parse_time_series(col), ref_time(col))


@pytest.mark.parametrize("v", [106752.0, 150000.0, 199999.0])
def test_serial_beyond_the_timedelta_range_still_stops_the_run(v):
    col = obj([46000.5, v])
    with pytest.raises(ValueError):
        ref_time(col)
    with pytest.raises(ValueError):
        rules.parse_time_series(col)


def test_time_native_dtypes():
    for col in (pd.Series([46000.5, NAN, 0.5, 200000.0, 46001.0]), pd.Series([46000, 46001, 0, 5]),
                pd.Series([46000.5, 46001.25], dtype="float32"),
                pd.Series(pd.to_datetime(["2026-03-04 10:11:12", None])),
                pd.Series(pd.to_datetime(["2026-03-04"])).astype("datetime64[s]"),
                pd.Series([], dtype=object), pd.Series([], dtype=float)):
        got, want = rules.parse_time_series(col), ref_time(col)
        same(got, want)
        assert got.dtype == "datetime64[ns]"


def test_time_keeps_the_index():
    col = pd.Series([46000.5, None], index=[7, 9], dtype=object)
    assert list(rules.parse_time_series(col).index) == [7, 9]


@pytest.mark.parametrize("v", PHONE_VALUES, ids=repr)
@pytest.mark.parametrize("digits,min_digits", [(10, 10), (7, 9)])
def test_phone_each_value_alone(v, digits, min_digits):
    col = obj([v])
    same(rules.norm_phone_series(col, digits, min_digits), ref_phone(col, digits, min_digits))


def test_phone_all_values_in_one_column():
    col = obj(PHONE_VALUES)
    same(rules.norm_phone_series(col), ref_phone(col))


def test_phone_native_dtypes_and_result_type():
    for col in (pd.Series([5551234567, 15551234567, 12]), pd.Series([5551234567.0, NAN, 1.5]),
                pd.Series(["5551234567", "x"]), pd.Series([], dtype=object)):
        got = rules.norm_phone_series(col)
        same(got, ref_phone(col))
        assert got.dtype == object


@pytest.mark.parametrize("v", TEXT_VALUES, ids=repr)
def test_text_each_value_alone(v):
    col = obj([v])
    same(rules.clean_text_series(col), col.map(rules.clean_text))


def test_text_all_values_in_one_column():
    col = obj(TEXT_VALUES)
    same(rules.clean_text_series(col), col.map(rules.clean_text))


@pytest.mark.parametrize("v", NUMBER_VALUES, ids=repr)
def test_number_each_value_alone(v):
    col = obj([v])
    same(rules.parse_number_series(col), col.map(rules.parse_number))


def test_number_all_values_in_one_column_and_native_dtypes():
    for col in (obj(NUMBER_VALUES), pd.Series([1.5, NAN, 3.0]), pd.Series([1, 2, 3]), pd.Series([True, False]),
                pd.Series([], dtype=object)):
        got = rules.parse_number_series(col)
        same(got, col.map(rules.parse_number) if len(col) else pd.Series([], dtype=float))
        assert got.dtype == np.float64


@pytest.mark.parametrize("v", BOOL_VALUES, ids=repr)
def test_bool_each_value_alone(v):
    col = obj([v])
    same(rules.parse_bool_series(col), col.map(rules.parse_bool))


def test_bool_all_values_in_one_column_and_native_dtype():
    for col in (obj(BOOL_VALUES), pd.Series([True, False, True])):
        got = rules.parse_bool_series(col)
        same(got, col.map(rules.parse_bool))
        assert got.dtype == bool


# ---------- randomised mixed columns ----------

def mixed_column(rng: np.random.Generator, n: int, kind: str) -> pd.Series:
    pools = {"time": TIME_VALUES, "phone": PHONE_VALUES, "text": TEXT_VALUES, "number": NUMBER_VALUES, "bool": BOOL_VALUES}
    odd = pools[kind]
    vals: list = []
    for i in range(n):
        r = rng.random()
        if r < 0.15:
            vals.append(odd[rng.integers(len(odd))])
        elif kind == "time":
            c = rng.integers(4)
            if c == 0:
                vals.append(float(46000 + rng.uniform(0, 400)))
            elif c == 1:
                vals.append(int(46000 + rng.integers(0, 400)))
            elif c == 2:
                vals.append(f"2026-{rng.integers(1, 14):02d}-{rng.integers(1, 32):02d} {rng.integers(0, 25):02d}:{rng.integers(0, 61):02d}:{rng.integers(0, 61):02d}")
            else:
                vals.append(None)
        elif kind == "phone":
            n10 = 5_550_000_000 + int(rng.integers(0, 10_000_000))
            c = rng.integers(5)
            vals.append([str(n10), f"+1{n10}", f"({str(n10)[:3]}) {str(n10)[3:6]}-{str(n10)[6:]}", n10, float(n10)][c])
        elif kind == "text":
            vals.append(["Open", " Closed ", "Active", None, 12][rng.integers(5)])
        elif kind == "number":
            vals.append([float(rng.uniform(0, 9e4)), int(rng.integers(0, 9e4)), f"${rng.uniform(0, 9e4):,.2f}", None, ""][rng.integers(5)])
        else:
            vals.append([True, False, "TRUE", "false", None][rng.integers(5)])
    return obj(vals)


@pytest.mark.parametrize("kind", ["time", "phone", "text", "number", "bool"])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_randomised_mixed_columns(kind, seed):
    col = mixed_column(np.random.default_rng(seed), 20_000, kind)
    if kind == "time":
        assert rules._parse_time_fast(col) is not None       # exercises the bulk path, not the scalar fallback
        same(rules.parse_time_series(col), ref_time(col))
    elif kind == "phone":
        same(rules.norm_phone_series(col), ref_phone(col))
    elif kind == "text":
        same(rules.clean_text_series(col), col.map(rules.clean_text))
    elif kind == "number":
        same(rules.parse_number_series(col), col.map(rules.parse_number))
    else:
        same(rules.parse_bool_series(col), col.map(rules.parse_bool))


def test_serial_dates_match_the_scalar_to_the_millisecond():
    rng = np.random.default_rng(11)
    x = np.concatenate([46000 + rng.uniform(0, 400, 40_000),
                        46000 + rng.integers(0, 400, 20_000) + rng.integers(0, 86_400_000, 20_000) / 86_400_000,
                        46000 + rng.integers(0, 400, 20_000) + (rng.integers(0, 86_400_000, 20_000) + 0.5) / 86_400_000,
                        rng.uniform(1, 106000, 20_000)])
    same(rules.parse_time_series(pd.Series(x)), ref_time(pd.Series(x, dtype=object)))


# ---------- whole prepare_* tables against the original cell-by-cell implementations ----------

def ref_prepare_ringba(df, cfg):
    c, p = cfg["sources"]["ringba"]["columns"], cfg["phone"]
    out = pd.DataFrame({
        "_row": df["_row"].values,
        "call_id": df[c["call_id"]].map(rules.clean_text).values,
        "call_time": df[c["call_time"]].map(rules.parse_time).values,
        "phone": df[c["phone"]].map(lambda v: rules.norm_phone(v, p["digits"], p["min_digits"])).values,
        "payout": df[c["payout"]].map(rules.parse_number).values,
        "connected": df[c["connected"]].map(rules.parse_bool).values,
        "converted": df[c["converted"]].map(rules.parse_bool).values,
    })
    out["call_time"] = pd.to_datetime(out["call_time"])
    return out


def ref_prepare_ringcx(df, cfg):
    c, p = cfg["sources"]["ringcx"]["columns"], cfg["phone"]
    aliases = {k.upper().strip(): v.upper().strip() for k, v in cfg["agent_match"]["aliases"].items()}
    agent = df[c["agent"]].map(rules.clean_text).str.upper()
    return pd.DataFrame({
        "_row": df["_row"].values,
        "date": pd.to_datetime(df[c["date"]].map(rules.parse_time)).dt.normalize().values,
        "result": df[c["result"]].map(rules.clean_text).values,
        "agent": agent.replace(aliases).values,
        "phone": df[c["phone"]].map(lambda v: rules.norm_phone(v, p["digits"], p["min_digits"])).values,
    })


def ref_prepare_lenders(tables, cfg):
    p, le = cfg["phone"], cfg["lenders"]
    frames = []
    for key in le["order"]:
        df, c = tables[key], cfg["sources"][key]["columns"]
        base = pd.DataFrame({
            "_row": df["_row"].values,
            "status": df[c["status"]].map(rules.clean_text).values,
            "close_date": pd.to_datetime(df[c["close_date"]].map(rules.parse_time)).values,
            "debt": (df[c["debt"]].map(rules.parse_number).values if c["debt"] else np.nan),
            "agent": df[c["agent"]].map(rules.clean_text).values,
        })
        seen = None
        for col in c["phones"]:
            ph = df[col].map(lambda v: rules.norm_phone(v, p["digits"], p["min_digits"])).values
            part = base.assign(phone=ph)
            if seen is not None:
                part = part[part["phone"] != seen]
            frames.append(part.assign(lender=key))
            seen = ph if seen is None else seen
    crm = pd.concat(frames, ignore_index=True)
    crm = crm[crm["phone"] != ""].copy()
    closed = {k: set(v) for k, v in le["closed_statuses"].items()}
    crm["is_closed"] = [s in closed[k] for k, s in zip(crm["lender"], crm["status"])]
    crm["is_active"] = crm["is_closed"] & ~crm["status"].isin(set(le["inactive_statuses"]))
    crm["close_date"] = pd.to_datetime(crm["close_date"])
    return crm


@pytest.fixture(scope="module")
def synthetic():
    cfg = load_config(CFG_PATH)
    return cfg, generate(20_000, seed=5, cfg=cfg)


def test_prepare_tables_equal_the_original_implementation(synthetic):
    cfg, t = synthetic
    pd.testing.assert_frame_equal(rules.prepare_ringba(t["ringba"], cfg), ref_prepare_ringba(t["ringba"], cfg), check_exact=True)
    pd.testing.assert_frame_equal(rules.prepare_ringcx(t["ringcx"], cfg), ref_prepare_ringcx(t["ringcx"], cfg), check_exact=True)
    pd.testing.assert_frame_equal(rules.prepare_lenders(t, cfg), ref_prepare_lenders(t, cfg), check_exact=True)


def test_status_helpers_equal_the_original_implementation(synthetic):
    cfg, t = synthetic
    le = cfg["lenders"]
    for key in le["order"]:
        col = cfg["sources"][key]["columns"]["status"]
        want = int(t[key][col].map(rules.clean_text).isin(set(le["closed_statuses"][key])).sum())
        assert rules.closed_status_rows(t, cfg)[key] == want
        seen = set(t[key][col].map(rules.clean_text))
        extra = sorted(seen - set(le["closed_statuses"][key]) - set(le["known_non_closing_statuses"][key]))
        assert rules.unknown_statuses(t, cfg).get(key, []) == extra


def test_dirty_cells_in_every_source_still_match_the_original(synthetic):
    """Blank, odd-typed and text cells scattered through all five sources."""
    cfg, t = synthetic
    rng = np.random.default_rng(9)
    dirty = {}
    for key, df in t.items():
        d = df.copy()
        for col in d.columns:
            if col == "_row":
                continue
            d[col] = d[col].astype(object)
            for i in rng.choice(len(d), size=300, replace=False):
                d.at[i, col] = [None, "", " ", NAN, "x", 7, 7.5, True, " 5551234567 ", "2026-03-04 10:00"][rng.integers(10)]
        dirty[key] = d
    pd.testing.assert_frame_equal(rules.prepare_ringba(dirty["ringba"], cfg), ref_prepare_ringba(dirty["ringba"], cfg), check_exact=True)
    pd.testing.assert_frame_equal(rules.prepare_ringcx(dirty["ringcx"], cfg), ref_prepare_ringcx(dirty["ringcx"], cfg), check_exact=True)
    pd.testing.assert_frame_equal(rules.prepare_lenders(dirty, cfg), ref_prepare_lenders(dirty, cfg), check_exact=True)
