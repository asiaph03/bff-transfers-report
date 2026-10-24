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
            # Serial days are floats: 16:33:48 arrives as 16:33:47.99999. Round to the millisecond.
            return (EXCEL_EPOCH + pd.to_timedelta(float(v), unit="D")).round("ms")
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
    out["close_row"] = pick["_row"].values          # the matched lender record (its sheet row), for closer lookup
    out["active"] = pick["is_active"].fillna(False).astype(bool).values
    excluded = m[m["is_closed"] & ~m["on_or_after"]]
    return out, excluded


# ---------- closing agent (separate from the answering agent) ----------

# Closed-deal closer statuses. Only "verified" carries a name; every other closed deal shows the unknown label.
# The closer comes only from the matched lender record's ownership fields, never from RingCX.
CLOSER_STATUSES = ("not_closed", "no_source", "missing", "non_bff", "conflicting", "partial", "unmapped", "verified")


def _fold(v: str) -> str:
    return " ".join(str(v).split()).casefold()


def _blank(v) -> str:
    """Ownership text as written; blank, NaN-like and None values are blank."""
    t = clean_text(v)
    return "" if t.casefold() in ("nan", "none") else t


def lender_closer_columns(cfg: dict) -> dict[str, list[str]]:
    """Extra lender columns the closer needs (ownership fields and any required column), per lender."""
    lm = cfg["closing_agent"]["lender_mapping"]
    out = {}
    for k, fields in lm["fields"].items():
        req = (lm.get("require") or {}).get(k)
        out[k] = list(dict.fromkeys(list(fields) + ([req["column"]] if req else [])))
    return out


def assign_closers(leads: pd.DataFrame, tables: dict | None, cfg: dict, use_lender: bool,
                   name_map: dict | None = None) -> pd.DataFrame:
    """Add closing_agent and closer_status from the matched lender record (match_closes' close_row).

    For a closed lead, the configured ownership field(s) of its matched lender record are read as written:
      all blank -> "missing"; any value is a listed system / lender-staff value, or the lender's required column
      (e.g. DebtBlue Assigned Company) does not hold an allowed value -> "non_bff"; two fields name different
      people -> "conflicting"; one field named and another blank (multi-field lenders) -> "partial";
      the name is not a verified agent name in `name_map` (after the approved agent_match aliases only)
      -> "unmapped"; otherwise "verified" with the mapped display name.
    `_closer_value` keeps the original value(s) for review. Leads that did not close: "not_closed". No source
    in use: "no_source". RingCX is never consulted.
    """
    ca = cfg["closing_agent"]
    lm = ca["lender_mapping"]
    unknown = ca["unknown_label"]
    nm = {_fold(k): v for k, v in (name_map or {}).items()}
    aliases = {_fold(k): _fold(v) for k, v in (cfg.get("agent_match", {}).get("aliases") or {}).items()}
    non_bff = {k: {_fold(v) for v in vals} for k, vals in (lm.get("non_bff_values") or {}).items()}
    require = lm.get("require") or {}
    index = {}
    if use_lender and tables:
        for k in lm["fields"]:
            if k in tables:
                index[k] = tables[k].set_index("_row")
    out = leads.copy()
    cols = {k: [] for k in ("closing_agent", "closer_status", "_closer_value", "_closer_other_resolves")}
    for r in out.itertuples(index=False):
        if not bool(r.closed):
            vals = (None, "not_closed", "", False)
        elif not use_lender or r.close_lender_key not in index:
            vals = (unknown, "no_source", "", False)
        else:
            k = r.close_lender_key
            rec = index[k].loc[r.close_row]
            if isinstance(rec, pd.DataFrame):          # duplicate row keys never happen; refuse rather than guess
                rec = rec.iloc[0:0]
            fields = lm["fields"][k]
            raw = [_blank(rec[f]) if f in rec.index else "" for f in fields]
            named = [v for v in raw if v]
            shown = " | ".join(named)
            staff = [v for v in named if _fold(v) in non_bff.get(k, set())]
            bff = [v for v in named if _fold(v) not in non_bff.get(k, set())]
            req = require.get(k)
            req_ok = req is None or _fold(_blank(rec.get(req["column"], ""))) in {_fold(x) for x in req["values"]}
            distinct = sorted({aliases.get(_fold(v), _fold(v)) for v in bff})
            # would another field settle it if a system/staff or blank value were ignored? (decision aid only)
            other = len(fields) > 1 and len(distinct) == 1 and req_ok and (bool(staff) or len(named) < len(fields))
            if not named:
                vals = (unknown, "missing", "", False)
            elif staff or not req_ok:
                vals = (unknown, "non_bff", shown, other)
            elif len(distinct) > 1:
                vals = (unknown, "conflicting", shown, False)
            elif len(named) < len(fields):
                vals = (unknown, "partial", shown, other)
            elif distinct[0] not in nm:
                vals = (unknown, "unmapped", shown, False)
            else:
                vals = (nm[distinct[0]], "verified", shown, False)
        for key, v in zip(cols, vals):
            cols[key].append(v)
    for k, v in cols.items():
        out[k] = v
    return out


def closer_counts(leads: pd.DataFrame, cfg: dict | None = None) -> dict:
    """Aggregate closer-quality counts for closed leads (counts only, no records)."""
    closed = leads[leads["closed"]]
    counts = {s: int((closed["closer_status"] == s).sum()) for s in CLOSER_STATUSES if s != "not_closed"}
    aliases = {_fold(k): _fold(v) for k, v in ((cfg or {}).get("agent_match", {}).get("aliases") or {}).items()}
    canon = lambda v: aliases.get(_fold(v), _fold(v))  # noqa: E731 - approved answering-agent aliases only
    am = (cfg or {}).get("agent_match", {})
    # answering-agent placeholders (paid before the RingCX log, no accepted call): there is no agent to compare
    labels = {_fold(x) for x in (am.get("label_before_log"), am.get("label_not_answered")) if x}
    v = closed[closed["closer_status"] == "verified"]
    has_answer = [canon(b) not in labels and bool(_blank(b)) for b in v["agent"]]
    counts["verified_differs_from_answering_agent"] = int(sum(h and canon(a) != canon(b)
                                                              for a, b, h in zip(v["closing_agent"], v["agent"], has_answer)))
    counts["verified_without_answering_agent"] = int(len(v) - sum(has_answer))
    return counts


def closer_diagnostics(leads: pd.DataFrame, cfg: dict) -> dict:
    """Per-lender aggregate review figures (counts and agent-name values only)."""
    closed = leads[leads["closed"]]
    out = {}
    for k in cfg["lenders"]["order"]:
        g = closed[closed["close_lender_key"] == k]
        st = g["closer_status"]
        c = closer_counts(g, cfg)
        out[k] = {
            "closed_deals": int(len(g)),
            "usable_ownership_value": int(st.isin(["unmapped", "verified"]).sum()),
            "missing": int((st == "missing").sum()),
            "system_or_non_bff": int((st == "non_bff").sum()),
            "conflicting_fields": int((st == "conflicting").sum()),
            "one_field_blank": int((st == "partial").sum()),
            "unmapped_name": int((st == "unmapped").sum()),
            "verified": int((st == "verified").sum()),
            "verified_differs_from_answering_agent": c["verified_differs_from_answering_agent"],
            "verified_same_as_answering_agent": c["verified"] - c["verified_differs_from_answering_agent"] - c["verified_without_answering_agent"],
            "verified_without_answering_agent": c["verified_without_answering_agent"],
            "other_field_would_resolve": int(g["_closer_other_resolves"].map(lambda x: x is True).sum()),
            "unmapped_values": {n: int(x) for n, x in g.loc[st == "unmapped", "_closer_value"].value_counts().sort_index().items()},
            "non_bff_values": {n: int(x) for n, x in g.loc[st == "non_bff", "_closer_value"].value_counts().sort_index().items()},
            "verified_values": {n: int(x) for n, x in g.loc[st == "verified", "_closer_value"].value_counts().sort_index().items()},
        }
    return out


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
