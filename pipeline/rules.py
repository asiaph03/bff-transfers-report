"""Business rules for the BFF Transfers report. Pure functions: tables in, tables out.

Every threshold, list and name comes from config/report.yaml.
"""
from __future__ import annotations

import datetime as dt
import re

import numpy as np
import pandas as pd

EXCEL_EPOCH = pd.Timestamp("1899-12-30")


class RuleError(ValueError):
    """The data cannot satisfy a business rule (for example fewer than 300 paid leads)."""


# ---------- parsing ----------

def norm_phone(v, digits: int = 10, min_digits: int = 10) -> str:
    """Keep the last `digits` digits; anything shorter than `min_digits` is blank."""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    d = re.sub(r"[^0-9]", "", str(v))
    return d[-digits:] if len(d) >= min_digits else ""


def parse_time(v):
    """A sheet value as a Timestamp: serial numbers, datetimes or ISO-like text. Blank or bad -> NaT."""
    if v is None or v == "":
        return pd.NaT
    if isinstance(v, (pd.Timestamp, dt.datetime)):
        return pd.Timestamp(v)
    if isinstance(v, dt.date):
        return pd.Timestamp(v)
    if isinstance(v, (int, float, np.integer, np.floating)):
        if isinstance(v, float) and np.isnan(v):
            return pd.NaT
        if 1 <= float(v) < 200000:  # spreadsheet serial day number
            return EXCEL_EPOCH + pd.to_timedelta(float(v), unit="D")
        return pd.NaT
    s = str(v).strip()
    if not s:
        return pd.NaT
    try:
        return pd.Timestamp(pd.to_datetime(s))
    except (ValueError, TypeError, OverflowError):
        return pd.NaT


def parse_number(v):
    if v is None or v == "":
        return np.nan
    if isinstance(v, (int, float, np.integer, np.floating)):
        return float(v)
    s = re.sub(r"[$,\s]", "", str(v))
    try:
        return float(s)
    except ValueError:
        return np.nan


def parse_bool(v) -> bool:
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    return str(v).strip().upper() == "TRUE"


def clean_text(v) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return ""
    return str(v).strip()


# ---------- paid leads ----------

def prepare_ringba(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    c, p = cfg["sources"]["ringba"]["columns"], cfg["phone"]
    out = pd.DataFrame({
        "_row": df["_row"].values,
        "call_id": df[c["call_id"]].map(clean_text).values,
        "call_time": df[c["call_time"]].map(parse_time).values,
        "phone": df[c["phone"]].map(lambda v: norm_phone(v, p["digits"], p["min_digits"])).values,
        "payout": df[c["payout"]].map(parse_number).values,
        "connected": df[c["connected"]].map(parse_bool).values,
        "converted": df[c["converted"]].map(parse_bool).values,
    })
    out["call_time"] = pd.to_datetime(out["call_time"])
    return out


def select_paid_leads(ringba: pd.DataFrame, cfg: dict) -> tuple[pd.DataFrame, dict]:
    """Latest paid call per phone (U1), then the newest `window_size` of those, newest first."""
    floor = cfg["paid_leads"]["min_payout_exclusive"]
    n = cfg["report"]["window_size"]
    with_phone = ringba[ringba["phone"] != ""]
    paid = with_phone[with_phone["payout"].fillna(0) > floor]
    diag = {
        "ringba_rows": int(len(ringba)),
        "ringba_rows_without_phone": int((ringba["phone"] == "").sum()),
        "paid_rows": int(len(paid)),
        "paid_rows_unparsed_time": int(paid["call_time"].isna().sum()),
    }
    paid = paid[paid["call_time"].notna()]
    latest = (paid.sort_values(["call_time", "call_id"], kind="mergesort")
                  .groupby("phone", sort=False).tail(1))
    diag["paid_unique_phones"] = int(len(latest))
    if len(latest) < n and not cfg["report"]["allow_fewer_than_window"]:
        raise RuleError(f"only {len(latest)} unique paid leads are available; the report needs {n}")
    window = latest.sort_values(["call_time", "call_id"], kind="mergesort").tail(n)
    calls = with_phone.groupby("phone").size()
    window = window.assign(ringba_calls=window["phone"].map(calls).astype(int))
    return window.iloc[::-1].reset_index(drop=True), diag


# ---------- agents ----------

def prepare_ringcx(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    c, p = cfg["sources"]["ringcx"]["columns"], cfg["phone"]
    aliases = {k.upper().strip(): v.upper().strip() for k, v in cfg["agent_match"]["aliases"].items()}
    agent = df[c["agent"]].map(clean_text).str.upper()
    out = pd.DataFrame({
        "_row": df["_row"].values,
        "date": pd.to_datetime(df[c["date"]].map(parse_time)).dt.normalize().values,
        "result": df[c["result"]].map(clean_text).values,
        "agent": agent.replace(aliases).values,
        "phone": df[c["phone"]].map(lambda v: norm_phone(v, p["digits"], p["min_digits"])).values,
    })
    return out


def match_agents(leads: pd.DataFrame, ringcx: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """First accepted RingCX row on or after the paid-call date; ties by sheet order (U2)."""
    am = cfg["agent_match"]
    log_start = pd.Timestamp(am["ringcx_log_start"])
    x = ringcx.merge(leads[["phone", "call_time"]], on="phone", how="inner")
    x = x[(x["date"] >= x["call_time"].dt.normalize()) & (x["result"] == am["accepted_result"])]
    first = x.sort_values(["date", "_row"], kind="mergesort").groupby("phone").head(1).set_index("phone")
    out = leads.copy()
    out["answered"] = out["phone"].isin(first.index)
    out["agent"] = out["phone"].map(first["agent"]).map(lambda a: a.title() if isinstance(a, str) and a else None)
    before = (~out["answered"]) & (out["call_time"].dt.normalize() < log_start)
    out.loc[before, "agent"] = am["label_before_log"]
    out["agent"] = out["agent"].fillna(am["label_not_answered"])
    return out


# ---------- lenders ----------

def prepare_lenders(tables: dict[str, pd.DataFrame], cfg: dict) -> pd.DataFrame:
    p, le = cfg["phone"], cfg["lenders"]
    frames = []
    for key in le["order"]:
        df, c = tables[key], cfg["sources"][key]["columns"]
        base = pd.DataFrame({
            "_row": df["_row"].values,
            "status": df[c["status"]].map(clean_text).values,
            "close_date": pd.to_datetime(df[c["close_date"]].map(parse_time)).values,
            "debt": (df[c["debt"]].map(parse_number).values if c["debt"] else np.nan),
            "agent": df[c["agent"]].map(clean_text).values,
        })
        seen = None
        for col in c["phones"]:
            ph = df[col].map(lambda v: norm_phone(v, p["digits"], p["min_digits"])).values
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


def unknown_statuses(tables: dict[str, pd.DataFrame], cfg: dict) -> dict[str, list[str]]:
    """Statuses on file that are neither closing nor known non-closing (U8)."""
    le, out = cfg["lenders"], {}
    for key in le["order"]:
        col = cfg["sources"][key]["columns"]["status"]
        seen = set(tables[key][col].map(clean_text))
        known = set(le["closed_statuses"][key]) | set(le["known_non_closing_statuses"][key])
        extra = sorted(seen - known)
        if extra:
            out[key] = extra
    return out


def closed_status_rows(tables: dict[str, pd.DataFrame], cfg: dict) -> dict[str, int]:
    """Rows on each lender tab whose status is on its closed list (compared with its Closed tab)."""
    out = {}
    for key in cfg["lenders"]["order"]:
        col = cfg["sources"][key]["columns"]["status"]
        out[key] = int(tables[key][col].map(clean_text).isin(set(cfg["lenders"]["closed_statuses"][key])).sum())
    return out


def match_closes(leads: pd.DataFrame, crm: pd.DataFrame, cfg: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Earliest qualifying close per lead, on or after the paid-call calendar date (U6)."""
    order = {k: i for i, k in enumerate(cfg["lenders"]["order"])}
    m = crm.merge(leads[["phone", "call_time"]], on="phone", how="inner")
    m["on_or_after"] = m["close_date"].notna() & (m["close_date"].dt.normalize() >= m["call_time"].dt.normalize())
    q = m[m["is_closed"] & m["on_or_after"]].assign(_lo=lambda d: d["lender"].map(order))
    first = q.sort_values(["close_date", "_lo", "_row"], kind="mergesort").groupby("phone").head(1).set_index("phone")
    out = leads.copy()
    out["in_lender_crm"] = out["phone"].isin(set(m["phone"]))
    for k in cfg["lenders"]["order"]:
        out[f"in_{k}"] = out["phone"].isin(set(m.loc[m["lender"] == k, "phone"]))
    out["closed"] = out["phone"].isin(first.index)
    pick = first.reindex(out["phone"])
    out["close_lender_key"] = pick["lender"].values
    out["close_status"] = pick["status"].fillna("").values
    out["close_date"] = pd.to_datetime(pick["close_date"]).values
    out["debt_enrolled"] = pd.to_numeric(pick["debt"], errors="coerce").values
    out["lender_agent"] = pick["agent"].values
    out["active"] = pick["is_active"].fillna(False).astype(bool).values
    excluded = m[m["is_closed"] & ~m["on_or_after"]]
    return out, excluded


# ---------- output rows ----------

def to_public_rows(leads: pd.DataFrame, cfg: dict) -> list[dict]:
    names = cfg["lenders"]["display_names"]
    pre = cfg["publish"]["lead_id_prefix"]
    rows = []
    for i, r in enumerate(leads.itertuples(index=False), start=1):
        t = r.call_time
        week = (t.normalize() - pd.Timedelta(days=t.weekday()))
        closed = bool(r.closed)
        cd = r.close_date if closed else pd.NaT
        la = r.lender_agent if closed and isinstance(r.lender_agent, str) else ""
        rows.append({
            "lead_id": f"{pre}{i:03d}",
            "paid_call": t.strftime("%Y-%m-%dT%H:%M:%S"),
            "week_start": week.strftime("%Y-%m-%d"),
            "month": t.strftime("%Y-%m"),
            "ringba_calls": int(r.ringba_calls),
            "connected": int(bool(r.connected)),
            "converted": int(bool(r.converted)),
            "payout": round(float(r.payout), 2),
            "answered": int(bool(r.answered)),
            "agent": r.agent,
            "in_lender_crm": int(bool(r.in_lender_crm)),
            "in_citizen": int(bool(r.in_citizen)),
            "in_cordoba": int(bool(r.in_cordoba)),
            "in_debtblue": int(bool(r.in_debtblue)),
            "closed": int(closed),
            "active": int(bool(r.active)),
            "close_lender": names.get(r.close_lender_key, "") if closed else "",
            "close_status": r.close_status if closed else "",
            "close_date": cd.strftime("%Y-%m-%dT%H:%M:%S") if closed and pd.notna(cd) else "",
            "days_to_close": int((cd.normalize() - t.normalize()).days) if closed and pd.notna(cd) else None,
            "debt_enrolled": (round(float(r.debt_enrolled), 2) if closed and pd.notna(r.debt_enrolled) else None),
            "lender_agent": (la.title() if la.isupper() else la),
        })
    return rows


def summarize(rows: list[dict]) -> dict:
    s = lambda k: sum((r[k] or 0) for r in rows)
    closed = [r for r in rows if r["closed"]]
    by_lender = {}
    for r in closed:
        by_lender[r["close_lender"]] = by_lender.get(r["close_lender"], 0) + 1
    times = sorted(r["paid_call"] for r in rows)
    return {
        "leads": len(rows), "connected": s("connected"), "converted": s("converted"), "answered": s("answered"),
        "in_lender_crm": s("in_lender_crm"), "closed": len(closed), "active": s("active"),
        "debt_enrolled": round(sum(r["debt_enrolled"] or 0 for r in closed), 2),
        "closes_with_debt": sum(1 for r in closed if r["debt_enrolled"] is not None),
        "payout": round(s("payout"), 2), "closes_by_lender": by_lender,
        "first_paid_call": times[0] if times else None, "last_paid_call": times[-1] if times else None,
    }


def months_without_calls(ringba: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> list[str]:
    have = set(ringba["call_time"].dropna().dt.strftime("%Y-%m"))
    months = pd.period_range(start.to_period("M"), end.to_period("M"), freq="M")
    return [str(p) for p in months if str(p) not in have]
