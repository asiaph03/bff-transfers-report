"""Synthetic source tables for repeatable benchmarks. Made-up data only: no real phones, names or amounts.

    python -m benchmarks.synth --rows 100000 --seed 1            # writes out/bench/data_100000_1.pkl

The tables have the shape GoogleSheetsSource.read() returns (sheet column names, a `_row` column, raw cell
values as the Sheets API gives them with UNFORMATTED_VALUE / SERIAL_NUMBER: numbers, text, booleans, None),
so every parsing and matching rule runs on the same kinds of values as a real refresh. Fetch time is not
modelled here: it needs real credentials and is measured separately (see the per-request log in sheets.py).
"""
from __future__ import annotations

import argparse
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

from pipeline.config import load_config

ROOT = Path(__file__).resolve().parent.parent
EXCEL_EPOCH = pd.Timestamp("1899-12-30")
MIN_ROWS = 20_000      # below this the newest-3% pool may hold fewer than the 300 paid leads the report needs

AGENTS = ["Ben Hall", "Ana Lopez", "Carl Diaz", "Dee Ford", "Eli Grant", "Fay Hill", "Gus Ivey", "Hal Jones", "Ivy King",
          "Jo Lane", "Kai Moss", "Lee Nash", "Max Owen", "Ned Page", "Oz Quinn", "Pat Reed", "Quin Sol", "Ray Tate"]
RESULTS = ["Inbound Accepted", "Inbound Abandoned", "Inbound Voicemail", "Inbound Transferred Out"]


def _serial(ts: pd.Timestamp) -> float:
    return (ts - EXCEL_EPOCH) / pd.Timedelta(days=1)


def _phone_text(rng: np.random.Generator, ids: np.ndarray, styles: tuple) -> list:
    """Phone cells in the formats real sheets hold: bare digits, +1 prefix, punctuation, or a number."""
    nums = 5_550_000_000 + ids
    kind = rng.choice(len(styles), size=len(ids))
    out = []
    for n, k in zip(nums.tolist(), kind.tolist()):
        s = styles[k]
        if s == "digits":
            out.append(str(n))
        elif s == "plus1":
            out.append(f"+1{n}")
        elif s == "punct":
            d = str(n)
            out.append(f"({d[:3]}) {d[3:6]}-{d[6:]}")
        elif s == "float":
            out.append(float(n))
        else:
            out.append(n)
    return out


def generate(rows: int, seed: int, cfg: dict | None = None) -> dict[str, pd.DataFrame]:
    if rows < MIN_ROWS:
        raise ValueError(f"use at least {MIN_ROWS} rows")
    cfg = cfg or load_config(ROOT / "config" / "report.yaml")
    rng = np.random.default_rng(seed)
    n = rows
    pool = max(5000, n // 2)
    src = cfg["sources"]

    # ---- Ringba: appended in time order, ~20% of calls paid ----
    start, end = pd.Timestamp("2026-01-01"), pd.Timestamp("2026-10-23")
    days = np.sort(rng.uniform(0, (end - start).days, n))
    t = np.round(_serial(start) + days, 6)
    ph = rng.integers(0, pool, n)
    paid = rng.random(n) < 0.2
    payout = np.where(paid, rng.choice([32.0, 35.0, 40.0, 55.5], n), np.where(rng.random(n) < 0.5, 0.0, np.nan))
    call_time: list = t.tolist()
    for i in rng.choice(n, size=n // 200, replace=False).tolist():     # a few text timestamps and blanks, as in the sheet
        if paid[i] or rng.random() < 0.5:
            call_time[i] = (start + pd.Timedelta(days=float(days[i]))).strftime("%Y-%m-%d %H:%M:%S")
        else:
            call_time[i] = None
    c = src["ringba"]["columns"]
    ringba = pd.DataFrame({
        c["call_id"]: [f"c{i:09d}" for i in range(n)],
        c["call_time"]: call_time,
        c["phone"]: _phone_text(rng, ph, ("plus1", "digits", "int")),
        c["payout"]: [None if np.isnan(p) else float(p) for p in payout],
        c["connected"]: (rng.random(n) < 0.7).tolist(),
        c["converted"]: (rng.random(n) < 0.3).tolist(),
    })
    ringba["_row"] = range(2, n + 2)

    # Phones of recent paid calls: the ones the report's newest-300 window draws from.
    last = slice(int(n * 0.97), n)
    hot = np.unique(ph[last][paid[last]])
    hot_day = {int(p): float(d) for p, d in zip(ph[last][paid[last]], t[last][paid[last]])}

    def pick_phones(m: int, share: float, frac: float) -> tuple[np.ndarray, np.ndarray]:
        """`share` of rows use a random `frac` of the hot phones, so only some leads are answered / closed."""
        is_hot = rng.random(m) < share
        subset = rng.choice(hot, max(1, int(len(hot) * frac)), replace=False)
        # everyone else comes from a separate, much larger id range, so overlap with Ringba is only the hot subset
        ids = np.where(is_hot, rng.choice(subset, m), rng.integers(pool, pool + 10 * n, m))
        base = np.array([hot_day.get(int(i), np.nan) for i in ids])
        return ids, base

    # ---- RingCX: sheet order is by date; hot phones repeat on the same day with several agents (U2 ties) ----
    ids, base = pick_phones(n, 0.006, 0.6)      # about 1-2 rows per hot phone, several on the same day
    date = np.where(np.isnan(base), rng.uniform(_serial(start), _serial(end), n), np.floor(base) + rng.choice([0, 0, 0, 1, 3], n))
    date = np.floor(date)
    order = np.argsort(date, kind="stable")                    # equal dates keep generation order = sheet order
    ids, date = ids[order], date[order]
    c = src["ringcx"]["columns"]
    ringcx = pd.DataFrame({
        c["date"]: date.tolist(),
        c["result"]: rng.choice(RESULTS, n, p=[0.55, 0.25, 0.15, 0.05]).tolist(),
        c["agent"]: rng.choice(AGENTS, n).tolist(),
        c["phone"]: _phone_text(rng, ids, ("digits", "plus1", "float")),
    })
    ringcx["_row"] = range(2, n + 2)

    # ---- lenders ----
    le = cfg["lenders"]
    tables = {"ringba": ringba, "ringcx": ringcx}
    for key in le["order"]:
        ids, base = pick_phones(n, 0.0015, 0.25)
        close = np.where(np.isnan(base), rng.uniform(_serial(start), _serial(end), n), np.floor(base) + rng.integers(-5, 40, n))
        closing = le["closed_statuses"][key]
        other = [s for s in le["known_non_closing_statuses"][key]]
        status = np.where(rng.random(n) < 0.35, rng.choice(closing, n), rng.choice(other, n))
        close_cells: list = np.floor(close).tolist()
        for i in rng.choice(n, size=n // 33, replace=False).tolist():
            close_cells[i] = None
        c = src[key]["columns"]
        data = {
            c["phones"][0]: _phone_text(rng, ids, ("punct", "digits", "plus1")),
            c["status"]: status.tolist(),
            c["close_date"]: close_cells,
            c["agent"]: rng.choice(AGENTS + ["BFF API", ""], n).tolist(),
        }
        for extra in c["phones"][1:]:
            data[extra] = _phone_text(rng, rng.integers(pool, pool + 10 * n, n), ("punct", "digits"))
        if c["debt"]:
            data[c["debt"]] = [None if rng.random() < 0.3 else round(float(v), 2) for v in rng.uniform(3000, 90000, n)]
        df = pd.DataFrame(data)
        df["_row"] = range(2, n + 2)
        tables[key] = df
    return tables


def cache_path(rows: int, seed: int, out: Path) -> Path:
    return out / f"data_{rows}_{seed}.pkl"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, required=True, help="rows per source")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", default=str(ROOT / "out" / "bench"))
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    tables = generate(a.rows, a.seed)
    p = cache_path(a.rows, a.seed, out)
    with open(p, "wb") as f:
        pickle.dump(tables, f, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"wrote {p} ({p.stat().st_size / 2**20:.0f} MB): " + ", ".join(f"{k} {len(v)}" for k, v in tables.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
