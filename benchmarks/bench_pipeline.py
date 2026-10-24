"""Time and fingerprint the reporting pipeline on synthetic data, one process per run.

    python -m benchmarks.synth --rows 100000 --seed 1
    python -m benchmarks.bench_pipeline --rows 100000 --seed 1 --label baseline
    python -m benchmarks.bench_pipeline --rows 100000 --seed 1 --label optimized --compare out/bench/baseline_100000_1.json

It runs the same functions, in the same order, as pipeline.build.run (parse, select, match, close, validate,
publish rows) on tables shaped like the Sheets reads. It never touches Google or the network, so it measures
calculation only; fetch time is separate. Peak memory is the process peak working set, which includes the
loaded source tables (as in a real refresh, where they are in memory after the reads).

Every intermediate table and the published rows are fingerprinted, so a faster implementation can be shown
to produce identical output, not just identical totals. --compare exits 1 on any difference.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pickle
import platform
import time
from pathlib import Path

import pandas as pd

from pipeline import rules, validate
from pipeline.config import load_config
from pipeline.timing import peak_memory_mb

from .synth import ROOT, cache_path


def digest(df: pd.DataFrame) -> str:
    """Order-sensitive fingerprint of every cell (NaN/NaT included) of a table."""
    h = pd.util.hash_pandas_object(df.reset_index(drop=True), index=False)
    return hashlib.sha256(h.values.tobytes() + "|".join(map(str, df.columns)).encode()).hexdigest()[:20]


def text_digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()[:20]


class Stages:
    def __init__(self):
        self.seconds: dict[str, float] = {}

    def run(self, name: str, fn, *args, **kw):
        t0 = time.perf_counter()
        try:
            return fn(*args, **kw)
        finally:
            self.seconds[name] = round(time.perf_counter() - t0, 4)


def run_pipeline(tables: dict, cfg: dict) -> dict:
    st, fp = Stages(), {}
    rep = validate.Report()

    def check_inputs():
        validate.check_sources(rep, tables, cfg)
        validate.check_statuses(rep, rules.unknown_statuses(tables, cfg))
        return rules.closed_status_rows(tables, cfg)

    closed_rows = st.run("validate_inputs", check_inputs)
    ringba = st.run("parse_ringba", rules.prepare_ringba, tables["ringba"], cfg)
    fp["ringba_parsed"] = digest(ringba)
    leads, diag = st.run("select_paid_leads", rules.select_paid_leads, ringba, cfg)
    fp["leads_selected"] = digest(leads)
    ringcx = st.run("parse_ringcx", rules.prepare_ringcx, tables["ringcx"], cfg)
    fp["ringcx_parsed"] = digest(ringcx)
    leads = st.run("match_agents", rules.match_agents, leads, ringcx, cfg)
    fp["leads_agents"] = digest(leads)
    crm = st.run("parse_lenders", rules.prepare_lenders, tables, cfg)
    fp["lenders_parsed"] = digest(crm)
    leads, excluded = st.run("match_closes", rules.match_closes, leads, crm, cfg)
    fp["leads_closes"] = digest(leads)

    def checks():
        validate.check_business(rep, leads, ringcx, excluded, cfg)
        return rules.months_without_calls(ringba, leads["call_time"].min(), leads["call_time"].max())

    gaps = st.run("validate_business", checks)

    def publish():
        rows = rules.to_public_rows(leads, cfg)
        totals = rules.summarize(rows)
        validate.check_output(rep, rows, totals, cfg)
        text = json.dumps({"rows": rows}, separators=(",", ":"))
        validate.check_privacy(rep, {"data.json": text})
        return rows, totals

    rows, totals = st.run("publish_rows", publish)
    fp["published_rows"] = text_digest(rows)
    fp["totals"] = text_digest(totals)
    fp["diagnostics"] = text_digest({"diag": diag, "gaps": gaps, "closed_status_rows": closed_rows,
                                     "excluded": int(len(excluded))})
    return {"stages_s": st.seconds, "fingerprints": fp, "totals": totals, "diag": diag,
            "checks_failed": rep.failures, "warnings": rep.warnings}


def compare(now: dict, ref: dict) -> list[str]:
    diffs = [f"{k}: {ref['fingerprints'].get(k)} -> {v}" for k, v in now["fingerprints"].items()
             if ref["fingerprints"].get(k) != v]
    if now["totals"] != ref["totals"]:
        diffs.append("totals differ")
    if now["checks_failed"] != ref["checks_failed"]:
        diffs.append("validation failures differ")
    return diffs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, required=True)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--label", default="run")
    ap.add_argument("--out", default=str(ROOT / "out" / "bench"))
    ap.add_argument("--compare", help="a result file from an earlier run; exit 1 if any fingerprint differs")
    a = ap.parse_args(argv)
    out = Path(a.out)
    with open(cache_path(a.rows, a.seed, out), "rb") as f:
        tables = pickle.load(f)
    mem_loaded = peak_memory_mb()
    cfg = load_config(ROOT / "config" / "report.yaml")
    t0 = time.perf_counter()
    res = run_pipeline(tables, cfg)
    total = round(time.perf_counter() - t0, 3)
    res.update({
        "label": a.label, "rows_per_source": a.rows, "seed": a.seed, "calculation_total_s": total,
        "peak_memory_mb_after_load": round(mem_loaded, 0) if mem_loaded else None,
        "peak_memory_mb": round(peak_memory_mb() or 0, 0) or None,
        "python": platform.python_version(), "pandas": pd.__version__, "platform": platform.platform(),
    })
    p = out / f"{a.label}_{a.rows}_{a.seed}.json"
    p.write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    print(f"{a.label}: {a.rows:,} rows/source, calculation {total}s, peak {res['peak_memory_mb']} MB "
          f"(after load {res['peak_memory_mb_after_load']} MB) -> {p}")
    for k, v in res["stages_s"].items():
        print(f"  {k:<20}{v:>9.3f}s")
    t = res["totals"]
    print(f"  leads {t['leads']}, answered {t['answered']}, closed {t['closed']}, active {t['active']}, "
          f"payout {t['payout']}, failed checks {len(res['checks_failed'])}")
    if res["checks_failed"]:
        print("  FAILED CHECKS: " + "; ".join(res["checks_failed"]))
    if a.compare:
        ref = json.loads(Path(a.compare).read_text(encoding="utf-8"))
        diffs = compare(res, ref)
        print("  compare with " + a.compare + ": " + ("IDENTICAL" if not diffs else "DIFFERENT"))
        for d in diffs:
            print("   - " + d)
        return 1 if diffs else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
