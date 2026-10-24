"""Peak memory and output of staged ingestion (pipeline.staged) against the default flow, on synthetic data.

    python -m benchmarks.synth --rows 500000 --seed 1          # the combined cache (as before)
    python -m benchmarks.bench_staged --rows 500000 --split    # one cache file per source (stands in for one Sheets read each)
    python -m benchmarks.bench_staged --rows 500000 --strategy current --label cur
    python -m benchmarks.bench_staged --rows 500000 --strategy staged  --label stg --compare out/bench/fp_filt_500000_1.json

current: load all five sources, then run the pipeline (what pipeline.build does by default).
staged:  pipeline.staged.ingest (the code behind `ingest.staged` / `--ingest staged`), then the same calculation.

Both strategies unpickle each source where a real run would read it from Google, so load time is understated and the
JSON response a real read holds while parsing is not included (see benchmarks/proto_json_read.py for that part).
"""
from __future__ import annotations

import argparse
import json
import pickle
import platform
import time
from pathlib import Path

import pandas as pd

from pipeline import rules, staged, validate
from pipeline.config import load_config
from pipeline.timing import peak_memory_mb

from .bench_pipeline import Stages, compare, digest, run_pipeline, text_digest
from .synth import ROOT, cache_path

SOURCES = ("ringba", "ringcx", "citizen", "cordoba", "debtblue")


def source_path(rows: int, seed: int, out: Path, key: str) -> Path:
    return out / f"src_{rows}_{seed}_{key}.pkl"


def load(rows: int, seed: int, out: Path, key: str) -> pd.DataFrame:
    with open(source_path(rows, seed, out, key), "rb") as f:
        return pickle.load(f)


def split(rows: int, seed: int, out: Path) -> None:
    with open(cache_path(rows, seed, out), "rb") as f:
        tables = pickle.load(f)
    for key, df in tables.items():
        with open(source_path(rows, seed, out, key), "wb") as f:
            pickle.dump(df, f, protocol=pickle.HIGHEST_PROTOCOL)
    print("wrote " + ", ".join(f"{k} {len(v):,} rows" for k, v in tables.items()))


class PickleSource:
    """Answers the calls GoogleSheetsSource.read / count_rows get, from the per-source cache files."""

    def __init__(self, cfg: dict, rows: int, seed: int, out: Path):
        self.by_sheet = {s["worksheet"]: k for k, s in cfg["sources"].items()}
        self.args = (rows, seed, out)

    def read(self, spreadsheet_id, worksheet, cols, whole_tab=False):
        return load(*self.args, self.by_sheet[worksheet])

    def count_rows(self, spreadsheet_id, worksheet, has_header):
        return 0          # the synthetic data has no Closed tabs; the benchmark does not use this number


def run_current(rows: int, seed: int, out: Path, cfg: dict, fingerprints: bool) -> dict:
    tables = {k: load(rows, seed, out, k) for k in SOURCES}
    return run_pipeline(tables, cfg, lender_filter=True, fingerprints=fingerprints)


def run_staged(rows: int, seed: int, out: Path, cfg: dict, fingerprints: bool) -> dict:
    st, fp = Stages(), {}
    rep = validate.Report()

    def fingerprint(name, fn):
        if fingerprints:
            fp[name] = fn()

    s = st.run("ingest", staged.ingest, cfg, PickleSource(cfg, rows, seed, out))
    assert s.rule_error is None, s.rule_error
    leads = s.leads
    fingerprint("leads_selected", lambda: digest(leads))
    leads = st.run("match_agents", rules.match_agents, leads, s.ringcx, cfg)
    fingerprint("leads_agents", lambda: digest(leads))
    crm = s.crm
    fingerprint("lenders_for_leads", lambda: digest(crm[crm["phone"].isin(set(leads["phone"]))]))
    leads, excluded = st.run("match_closes", rules.match_closes, leads, crm, cfg)
    fingerprint("leads_closes", lambda: digest(leads))

    validate.check_sources(rep, {k: range(n) for k, n in s.sizes.items()}, cfg)
    validate.check_statuses(rep, s.unknown)
    validate.check_business(rep, leads, s.ringcx, excluded, cfg)

    def publish():
        pub = rules.to_public_rows(leads, cfg)
        totals = rules.summarize(pub)
        validate.check_output(rep, pub, totals, cfg)
        validate.check_privacy(rep, {"data.json": json.dumps({"rows": pub}, separators=(",", ":"))})
        return pub, totals

    pub, totals = st.run("publish_rows", publish)
    fingerprint("published_rows", lambda: text_digest(pub))
    fingerprint("totals", lambda: text_digest(totals))
    fingerprint("diagnostics", lambda: text_digest({"diag": s.diag, "gaps": s.gaps, "closed_status_rows": s.closed_rows,
                                                    "excluded": int(len(excluded))}))
    return {"stages_s": st.seconds, "fingerprints": fp, "totals": totals, "diag": s.diag,
            "checks_failed": rep.failures, "warnings": rep.warnings}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, required=True)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", default=str(ROOT / "out" / "bench"))
    ap.add_argument("--split", action="store_true", help="write one cache file per source and stop")
    ap.add_argument("--strategy", choices=("current", "staged"))
    ap.add_argument("--label", default="run")
    ap.add_argument("--fingerprints", action="store_true", help="compute digests (they add memory of their own)")
    ap.add_argument("--compare", help="a result file from bench_pipeline or this tool; exit 1 on any difference")
    a = ap.parse_args(argv)
    out = Path(a.out)
    if a.split:
        split(a.rows, a.seed, out)
        return 0
    cfg = load_config(ROOT / "config" / "report.yaml")
    t0 = time.perf_counter()
    res = (run_staged if a.strategy == "staged" else run_current)(a.rows, a.seed, out, cfg, a.fingerprints)
    total = round(time.perf_counter() - t0, 3)
    res.update({"label": a.label, "strategy": a.strategy, "rows_per_source": a.rows, "seed": a.seed,
                "total_with_loading_s": total, "peak_memory_mb": round(peak_memory_mb() or 0), "digests_on": a.fingerprints,
                "python": platform.python_version(), "pandas": pd.__version__})
    p = out / f"{a.label}_{a.rows}_{a.seed}.json"
    p.write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    t = res["totals"]
    print(f"{a.label} [{a.strategy}] {a.rows:,} rows/source: {total}s including loading, peak {res['peak_memory_mb']} MB; "
          f"leads {t['leads']}, answered {t['answered']}, closed {t['closed']}, failed checks {len(res['checks_failed'])}")
    if a.compare:
        ref = json.loads(Path(a.compare).read_text(encoding="utf-8"))
        shared = [k for k in res["fingerprints"] if k in ref["fingerprints"]]
        diffs = compare(res, ref)
        print(f"  compare with {Path(a.compare).name}: {'IDENTICAL' if not diffs else 'DIFFERENT'} "
              f"({len(shared)} fingerprints compared: {', '.join(shared)})")
        for d in diffs:
            print("   - " + d)
        return 1 if diffs else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
