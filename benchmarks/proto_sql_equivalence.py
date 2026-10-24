"""PROTOTYPE, not production code. Run:  python -m benchmarks.proto_sql_equivalence

(SQLite, in memory): is a set-based SQL formulation of the three selection rules exactly the pandas rules?

Parsing and normalisation stay in Python (rules.prepare_*); SQL gets the already-normalised staging rows and does only:
  1. paid leads   : latest paid call per phone, newest N, calls-per-phone          (rules.select_paid_leads)
  2. answering agent: first accepted call on/after the paid date, ties by source_seq (rules.match_agents)
  3. closes       : earliest qualifying close, ties by lender order then sheet row  (rules.match_closes)
Portable window-function SQL only (ROW_NUMBER), so it should also run on PostgreSQL; PostgreSQL itself is NOT tested here.
"""
import copy
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from benchmarks.synth import ROOT, generate
from pipeline import rules
from pipeline.config import load_config
from pipeline.test_lender_filter import lender_tables, random_scenario, serial

cfg0 = load_config(ROOT / "config" / "report.yaml")
TS = "%Y-%m-%d %H:%M:%S.%f"


def ts(s: pd.Series):
    return [None if pd.isna(v) else v.strftime(TS) for v in s]


def day(s: pd.Series):
    return [None if pd.isna(v) else v.strftime("%Y-%m-%d") for v in s]


SHUFFLE = np.random.default_rng(99)


def shuffled(df):
    """Rows in a random physical order: only an explicit tie-break column can still give the sheet-order answer."""
    return df.iloc[SHUFFLE.permutation(len(df))]


def stage(conn, ringba=None, ringcx=None, crm=None, cfg=None):
    ringba = None if ringba is None else shuffled(ringba)
    ringcx = None if ringcx is None else shuffled(ringcx)
    crm = None if crm is None else shuffled(crm)
    if ringba is not None:
        conn.execute("DROP TABLE IF EXISTS ringba")
        conn.execute("CREATE TABLE ringba(sheet_row INT, call_id TEXT, call_time TEXT, phone TEXT, payout REAL, connected INT, converted INT)")
        conn.executemany("INSERT INTO ringba VALUES (?,?,?,?,?,?,?)", zip(
            ringba["_row"].tolist(), ringba["call_id"], ts(ringba["call_time"]), ringba["phone"],
            [None if pd.isna(v) else float(v) for v in ringba["payout"]], ringba["connected"].astype(int), ringba["converted"].astype(int)))
    if ringcx is not None:
        conn.execute("DROP TABLE IF EXISTS ringcx")
        conn.execute("CREATE TABLE ringcx(source_seq INT, day TEXT, result TEXT, agent TEXT, phone TEXT)")
        conn.executemany("INSERT INTO ringcx VALUES (?,?,?,?,?)", zip(
            ringcx["_row"].tolist(), day(ringcx["date"]), ringcx["result"], ringcx["agent"], ringcx["phone"]))
    if crm is not None:
        order = {k: i for i, k in enumerate(cfg["lenders"]["order"])}
        conn.execute("DROP TABLE IF EXISTS crm")
        conn.execute("CREATE TABLE crm(lender TEXT, lender_ord INT, sheet_row INT, status TEXT, close_ts TEXT, debt REAL, is_closed INT, is_active INT, phone TEXT)")
        conn.executemany("INSERT INTO crm VALUES (?,?,?,?,?,?,?,?,?)", zip(
            crm["lender"], [order[k] for k in crm["lender"]], crm["_row"].tolist(), crm["status"], ts(crm["close_date"]),
            [None if pd.isna(v) else float(v) for v in crm["debt"]], crm["is_closed"].astype(int), crm["is_active"].astype(int), crm["phone"]))


def sql_paid_leads(conn, n):
    return pd.read_sql_query("""
        WITH wp AS (SELECT * FROM ringba WHERE phone <> ''),
        paid AS (SELECT * FROM wp WHERE COALESCE(payout, 0) > 0 AND call_time IS NOT NULL),
        latest AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY phone ORDER BY call_time DESC, call_id DESC, sheet_row DESC) AS rn FROM paid),
        win AS (SELECT * FROM latest WHERE rn = 1 ORDER BY call_time DESC, call_id DESC, sheet_row DESC LIMIT ?),
        calls AS (SELECT phone, COUNT(*) AS n FROM wp GROUP BY phone)
        SELECT win.phone, win.call_id, win.call_time, win.payout, win.connected, win.converted, calls.n AS ringba_calls
        FROM win JOIN calls USING (phone) ORDER BY win.call_time DESC, win.call_id DESC, win.sheet_row DESC""", conn, params=(n,))


def sql_agents(conn):
    return pd.read_sql_query("""
        SELECT phone, agent FROM (
          SELECT l.phone, x.agent, ROW_NUMBER() OVER (PARTITION BY l.phone ORDER BY x.day, x.source_seq) AS rn
          FROM leads l JOIN ringcx x ON x.phone = l.phone
          WHERE x.result = ? AND x.day >= substr(l.call_time, 1, 10)) WHERE rn = 1""", conn, params=(cfg0["agent_match"]["accepted_result"],))


def sql_closes(conn):
    return pd.read_sql_query("""
        SELECT phone, lender, sheet_row, status, close_ts, debt, is_active FROM (
          SELECT l.phone, c.lender, c.sheet_row, c.status, c.close_ts, c.debt, c.is_active,
                 ROW_NUMBER() OVER (PARTITION BY l.phone ORDER BY c.close_ts, c.lender_ord, c.sheet_row) AS rn
          FROM leads l JOIN crm c ON c.phone = l.phone
          WHERE c.is_closed = 1 AND c.close_ts IS NOT NULL AND substr(c.close_ts, 1, 10) >= substr(l.call_time, 1, 10)) WHERE rn = 1""", conn)


def sql_in_crm(conn):
    return pd.read_sql_query("""
        SELECT l.phone, EXISTS(SELECT 1 FROM crm c WHERE c.phone = l.phone) AS in_crm,
               (SELECT COUNT(DISTINCT c.phone) FROM crm c JOIN leads l2 ON l2.phone = c.phone WHERE c.is_closed = 1
                  AND (c.close_ts IS NULL OR substr(c.close_ts,1,10) < substr(l2.call_time,1,10))) AS excluded_phones
        FROM leads l""", conn)


def compare(cfg, ringba_p, ringcx_p, crm_full, label):
    """Run pandas and SQL on the same normalised rows; return a list of differences (empty = identical)."""
    n = cfg["report"]["window_size"]
    try:
        ref_leads, _ = rules.select_paid_leads(ringba_p, cfg)
    except rules.RuleError:
        return None                                               # too few paid leads for this window: nothing to compare
    ref_a = rules.match_agents(ref_leads, ringcx_p, cfg)
    ref_c, excluded = rules.match_closes(ref_a, crm_full, cfg)
    conn = sqlite3.connect(":memory:")
    stage(conn, ringba_p, ringcx_p, crm_full, cfg)
    diffs = []
    sl = sql_paid_leads(conn, n)
    same_phones = list(sl["phone"]) == list(ref_leads["phone"]) and list(sl["call_id"]) == list(ref_leads["call_id"])
    if not same_phones or list(sl["ringba_calls"]) != list(ref_leads["ringba_calls"]):
        diffs.append("paid leads (order, ids or calls-per-phone) differ")
    # stage the selected leads for the next two queries
    conn.execute("DROP TABLE IF EXISTS leads")
    conn.execute("CREATE TABLE leads(phone TEXT, call_time TEXT)")
    conn.executemany("INSERT INTO leads VALUES (?,?)", zip(sl["phone"], sl["call_time"]))
    ag = sql_agents(conn).set_index("phone")["agent"]
    sql_agent = sl["phone"].map(ag)
    want_ans = ref_a["answered"].to_numpy()
    if list(sql_agent.notna()) != list(want_ans):
        diffs.append("answered flags differ")
    else:
        got = sql_agent.map(lambda a: a.title() if isinstance(a, str) and a else None)
        have = ref_a["phone"].map(lambda p: None)  # placeholder to keep shapes obvious
        ref_titled = ref_a["agent"].where(ref_a["answered"], None)
        ref_titled = ref_titled.where(~ref_a["agent"].isin([cfg["agent_match"]["label_before_log"], cfg["agent_match"]["label_not_answered"]]), None)
        if list(got.where(got.notna(), None)) != list(ref_titled.where(ref_titled.notna(), None)):
            diffs.append("answering agent differs")
    cl = sql_closes(conn).set_index("phone")
    closed = sl["phone"].isin(cl.index)
    if list(closed) != list(ref_c["closed"]):
        diffs.append("closed flags differ")
    for col_sql, col_ref, conv in (("lender", "close_lender_key", None), ("status", "close_status", None), ("sheet_row", "close_row", float),
                                   ("debt", "debt_enrolled", float), ("is_active", "active", bool)):
        got = sl["phone"].map(cl[col_sql]) if len(cl) else pd.Series([None] * len(sl))
        want = ref_c[col_ref]
        for g, w, c in zip(got, want, closed):
            if not c:
                continue
            gv = conv(g) if (conv and g is not None and not pd.isna(g)) else g
            wv = conv(w) if (conv and w is not None and not pd.isna(w)) else w
            if not ((pd.isna(gv) and pd.isna(wv)) or gv == wv):
                diffs.append(f"closes: {col_sql} differs"); break
    got_dt = pd.to_datetime(sl["phone"].map(cl["close_ts"])) if len(cl) else pd.Series(pd.NaT, index=sl.index)
    for g, w, c in zip(got_dt, ref_c["close_date"], closed):
        if c and g != w:
            diffs.append("closes: close_date differs"); break
    ic = sql_in_crm(conn)
    if list(ic["in_crm"].astype(bool)) != list(ref_c["in_lender_crm"]):
        diffs.append("in_lender_crm differs")
    if int(ic["excluded_phones"].max() or 0) != int(excluded["phone"].nunique()):
        diffs.append("excluded count differs")
    conn.close()
    return diffs


def ties_in(ringcx_p, leads):
    x = ringcx_p[(ringcx_p["result"] == cfg0["agent_match"]["accepted_result"]) & ringcx_p["phone"].isin(set(leads["phone"]))]
    g = x.groupby(["phone", "date"])["agent"].nunique()
    return int((g > 1).sum())


# ---- A. large synthetic data through the whole chain ----
for rows in (20_000, 100_000):
    t = generate(rows, seed=7, cfg=cfg0)
    ringba_p, ringcx_p = rules.prepare_ringba(t["ringba"], cfg0), rules.prepare_ringcx(t["ringcx"], cfg0)
    crm_full = rules.prepare_lenders(t, cfg0)
    leads, _ = rules.select_paid_leads(ringba_p, cfg0)
    d = compare(cfg0, ringba_p, ringcx_p, crm_full, f"synthetic {rows}")
    print(f"A. synthetic {rows:>7,} rows/source: {'IDENTICAL' if not d else d}; same-day multi-agent tie cases among the 300 leads: {ties_in(ringcx_p, leads)}")

# ---- B. random collision cases: lender closes (150 seeds) ----
bad = 0
for seed in range(150):
    tables, leads_df = random_scenario(cfg0, np.random.default_rng(seed))
    crm_full = rules.prepare_lenders(tables, cfg0)
    conn = sqlite3.connect(":memory:")
    stage(conn, crm=crm_full, cfg=cfg0)
    conn.execute("CREATE TABLE leads(phone TEXT, call_time TEXT)")
    conn.executemany("INSERT INTO leads VALUES (?,?)", zip(leads_df["phone"], ts(leads_df["call_time"])))
    ref, excl = rules.match_closes(leads_df, crm_full, cfg0)
    cl = sql_closes(conn).set_index("phone")
    ok = list(leads_df["phone"].isin(cl.index)) == list(ref["closed"])
    for ph, c, lk, row, st_, act in zip(ref["phone"], ref["closed"], ref["close_lender_key"], ref["close_row"], ref["close_status"], ref["active"]):
        if c:
            r = cl.loc[ph]
            ok &= (r["lender"] == lk and int(r["sheet_row"]) == int(row) and r["status"] == st_ and bool(r["is_active"]) == bool(act))
    bad += (not ok)
    conn.close()
print(f"B. 150 random lender-collision cases (formats, two phone columns, tied dates, odd statuses): {'all IDENTICAL' if not bad else str(bad) + ' DIFFER'}")

# ---- C. random paid-lead and RingCX tie cases (150 seeds) ----
c_bad, tie_cases = [], 0
cfg = copy.deepcopy(cfg0)
cfg["report"]["window_size"] = 6
cfg["report"]["allow_fewer_than_window"] = False
for seed in range(150):
    rng = np.random.default_rng(1000 + seed)
    pool, n = 14, 90
    cols = cfg["sources"]["ringba"]["columns"]
    times = [serial("2026-05-01") + float(v) for v in rng.integers(0, 5, n)] if seed % 2 else [serial("2026-05-01") + float(rng.integers(0, 6)) / 4 for _ in range(n)]
    ringba = pd.DataFrame({
        cols["call_id"]: [f"id{int(rng.integers(0, 6))}" for _ in range(n)],                 # repeated ids on purpose
        cols["call_time"]: [None if rng.random() < 0.05 else v for v in times],
        cols["phone"]: [f"555000{int(rng.integers(0, pool)):04d}" for _ in range(n)],
        cols["payout"]: [[0, 0, None, 32.0, 35.0][int(rng.integers(5))] for _ in range(n)],
        cols["connected"]: [True] * n, cols["converted"]: [False] * n}, dtype=object)
    ringba["_row"] = range(2, n + 2)
    rc = cfg["sources"]["ringcx"]["columns"]
    m = 120
    ringcx = pd.DataFrame({
        rc["date"]: [serial("2026-05-01") + float(rng.integers(0, 6)) for _ in range(m)],
        rc["result"]: [["Inbound Accepted", "Inbound Accepted", "Inbound Abandoned"][int(rng.integers(3))] for _ in range(m)],
        rc["agent"]: [["Ana Lopez", "Ben Hall", "benjamin hall", "Cy Diaz"][int(rng.integers(4))] for _ in range(m)],
        rc["phone"]: [f"555000{int(rng.integers(0, pool)):04d}" for _ in range(m)]}, dtype=object)
    ringcx["_row"] = range(2, m + 2)
    ringba_p, ringcx_p = rules.prepare_ringba(ringba, cfg), rules.prepare_ringcx(ringcx, cfg)
    tables, _ = random_scenario(cfg, np.random.default_rng(seed))
    crm_full = rules.prepare_lenders(tables, cfg)
    try:
        lds, _ = rules.select_paid_leads(ringba_p, cfg)
        tie_cases += ties_in(ringcx_p, lds)
    except rules.RuleError:
        continue
    d = compare(cfg, ringba_p, ringcx_p, crm_full, f"random {seed}")
    if d:
        c_bad.append((seed, d))
print(f"C. 150 random paid-lead / answering-agent tie cases ({tie_cases} same-day multi-agent ties seen): {'all IDENTICAL' if not c_bad else c_bad[:3]}")
