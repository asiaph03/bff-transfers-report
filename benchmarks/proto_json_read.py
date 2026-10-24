"""PROTOTYPE, not production code. Run:  python -m benchmarks.proto_json_read gen|whole|blocks_gen|blocks N [B]

How much memory does reading one Sheets-shaped response take: whole response vs row blocks? (Ringba columns, synthetic data.)

    gen   N        : write the response body GoogleSheetsSource.read would receive (batchGet, COLUMNS) for N rows
    whole N        : r.text -> json.loads -> DataFrame, as sheets.py does today
    blocks N B     : the same, B rows at a time, each block parsed and dropped after keeping only its parsed table's length
"""
import json
import pickle
import sys
import time
from pathlib import Path

import pandas as pd
from pipeline.timing import peak_memory_mb

from benchmarks.synth import ROOT

OUT = ROOT / "out" / "bench"
mode, n = sys.argv[1], int(sys.argv[2])
cols = ["inboundCallId", "callDtReadable", "inboundPhoneNumber", "buyerConversionAmount", "buyerHasConnected", "buyerHasConverted"]
body_path = lambda rows: OUT / f"sheets_body_{rows}.json"       # noqa: E731

if mode == "gen":
    df = pickle.load(open(OUT / f"src_{n}_1_ringba.pkl", "rb"))
    vr = [{"range": f"'Ringba2'!{chr(65 + i)}2:{chr(65 + i)}{n + 1}", "majorDimension": "COLUMNS", "values": [df[c].tolist()]} for i, c in enumerate(cols)]
    body_path(n).write_text(json.dumps({"spreadsheetId": "x", "valueRanges": vr}), encoding="utf-8")
    print(f"body file {body_path(n).stat().st_size / 2**20:.0f} MB for {n:,} rows x {len(cols)} columns")
elif mode == "whole":
    t0 = time.perf_counter()
    text = body_path(n).read_text(encoding="utf-8")                   # the HTTP body as text (requests holds this too)
    body = json.loads(text)
    cols_data = [vr["values"][0] for vr in body["valueRanges"]]
    df = pd.DataFrame({c: v for c, v in zip(cols, cols_data)})
    print(f"whole  {n:,} rows: {time.perf_counter() - t0:.1f}s, peak {peak_memory_mb():.0f} MB, rows {len(df):,}")
elif mode == "blocks_gen":
    b = int(sys.argv[3])
    full = json.loads(body_path(n).read_text(encoding="utf-8"))["valueRanges"]
    columns = [vr["values"][0] for vr in full]
    d = OUT / f"sheets_blocks_{n}_{b}"; d.mkdir(exist_ok=True)
    for i, lo in enumerate(range(0, n, b)):
        (d / f"{i:04d}.json").write_text(json.dumps([c[lo:lo + b] for c in columns]), encoding="utf-8")
    print(f"wrote {len(list(d.iterdir()))} block files of up to {b:,} rows")
elif mode == "blocks":
    b = int(sys.argv[3])
    d = OUT / f"sheets_blocks_{n}_{b}"
    t0, total = time.perf_counter(), 0
    for f in sorted(d.iterdir()):                      # one request at a time: its text, then its parsed table, then dropped
        block = json.loads(f.read_text(encoding="utf-8"))
        df = pd.DataFrame({c: v for c, v in zip(cols, block)})
        total += len(df)
        del block, df
    print(f"blocks {n:,} rows in {len(list(d.iterdir()))} blocks of {b:,}: {time.perf_counter() - t0:.1f}s, peak {peak_memory_mb():.0f} MB, rows {total:,}")
