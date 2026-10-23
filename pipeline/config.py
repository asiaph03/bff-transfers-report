"""Load and check config/report.yaml. A bad config stops the run before any data is read."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import yaml

SOURCE_KEYS = ("ringba", "ringcx", "citizen", "cordoba", "debtblue")
LENDER_KEYS = ("citizen", "cordoba", "debtblue")
REQUIRED_COLUMNS = {
    "ringba": ("call_id", "call_time", "phone", "payout", "connected", "converted"),
    "ringcx": ("date", "result", "agent", "phone"),
    "lender": ("phones", "status", "close_date", "debt", "agent"),
}


class ConfigError(ValueError):
    pass


def _need(cond: bool, msg: str, errors: list[str]) -> None:
    if not cond:
        errors.append(msg)


def validate_config(cfg: dict) -> list[str]:
    """Return a list of problems; an empty list means the config is usable."""
    e: list[str] = []
    if not isinstance(cfg, dict):
        return ["config is not a mapping"]
    _need(cfg.get("version") == 1, "version must be 1", e)
    rep = cfg.get("report", {})
    _need(isinstance(rep.get("window_size"), int) and rep.get("window_size", 0) > 0, "report.window_size must be a positive integer", e)
    _need(isinstance(rep.get("allow_fewer_than_window"), bool), "report.allow_fewer_than_window must be true or false", e)
    ph = cfg.get("phone", {})
    _need(isinstance(ph.get("digits"), int) and 7 <= ph.get("digits", 0) <= 15, "phone.digits must be 7-15", e)
    _need(isinstance(ph.get("min_digits"), int) and ph.get("min_digits", 0) >= ph.get("digits", 99), "phone.min_digits must be >= phone.digits", e)

    src = cfg.get("sources", {})
    for k in SOURCE_KEYS:
        s = src.get(k)
        if not isinstance(s, dict):
            e.append(f"sources.{k} is missing")
            continue
        _need(isinstance(s.get("spreadsheet_id"), str) and len(s["spreadsheet_id"]) > 20, f"sources.{k}.spreadsheet_id looks wrong", e)
        _need(isinstance(s.get("worksheet"), str) and s["worksheet"], f"sources.{k}.worksheet is missing", e)
        cols = s.get("columns", {})
        req = REQUIRED_COLUMNS["lender"] if k in LENDER_KEYS else REQUIRED_COLUMNS[k]
        for c in req:
            _need(c in cols, f"sources.{k}.columns.{c} is missing", e)
        if k in LENDER_KEYS:
            _need(isinstance(cols.get("phones"), list) and cols.get("phones"), f"sources.{k}.columns.phones must be a non-empty list", e)
            _need(cols.get("debt") is None or isinstance(cols.get("debt"), str), f"sources.{k}.columns.debt must be a column name or null", e)
        _need(isinstance(s.get("min_rows"), int) and s.get("min_rows", -1) >= 0, f"sources.{k}.min_rows must be a non-negative integer", e)

    pl = cfg.get("paid_leads", {})
    _need(isinstance(pl.get("min_payout_exclusive"), (int, float)), "paid_leads.min_payout_exclusive must be a number", e)
    _need(pl.get("dedupe") == "latest_paid_call_per_phone", "paid_leads.dedupe must be latest_paid_call_per_phone", e)
    _need(pl.get("sort_keys") == ["call_time", "call_id"], "paid_leads.sort_keys must be [call_time, call_id]", e)

    am = cfg.get("agent_match", {})
    _need(isinstance(am.get("accepted_result"), str) and am.get("accepted_result"), "agent_match.accepted_result is missing", e)
    _need(am.get("rule") == "first_on_or_after_paid_date", "agent_match.rule must be first_on_or_after_paid_date", e)
    _need(isinstance(am.get("aliases"), dict), "agent_match.aliases must be a mapping", e)
    start = am.get("ringcx_log_start")
    _need(isinstance(start, (dt.date, str)), "agent_match.ringcx_log_start must be a date", e)
    if isinstance(start, str):
        try:
            dt.date.fromisoformat(start)
        except ValueError:
            e.append("agent_match.ringcx_log_start must be YYYY-MM-DD")

    le = cfg.get("lenders", {})
    _need(sorted(le.get("order", [])) == sorted(LENDER_KEYS), "lenders.order must list citizen, cordoba, debtblue", e)
    for key in ("closed_statuses", "known_non_closing_statuses"):
        for k in LENDER_KEYS:
            v = le.get(key, {}).get(k)
            _need(isinstance(v, list) and all(isinstance(x, str) for x in v), f"lenders.{key}.{k} must be a list of text", e)
    for k in LENDER_KEYS:
        both = set(le.get("closed_statuses", {}).get(k, [])) & set(le.get("known_non_closing_statuses", {}).get(k, []))
        _need(not both, f"lenders: {k} status listed as both closing and non-closing: {sorted(both)}", e)
    _need(isinstance(le.get("inactive_statuses"), list), "lenders.inactive_statuses must be a list", e)
    _need(le.get("close_must_be_on_or_after_paid_date") is True, "lenders.close_must_be_on_or_after_paid_date must be true", e)
    _need(le.get("count_each_lead_once") == "earliest_close_date", "lenders.count_each_lead_once must be earliest_close_date", e)

    va = cfg.get("validation", {})
    bl = va.get("baseline", {})
    _need(isinstance(bl.get("required"), bool), "validation.baseline.required must be true or false", e)
    for m, t in (bl.get("metrics") or {}).items():
        _need(isinstance(t, dict) and isinstance(t.get("max_abs_change"), (int, float)) and isinstance(t.get("max_pct_change"), (int, float)),
              f"validation.baseline.metrics.{m} needs max_abs_change and max_pct_change", e)

    pub = cfg.get("publish", {})
    _need(isinstance(pub.get("site_url"), str) and pub["site_url"].startswith("https://"), "publish.site_url must be https", e)
    _need(isinstance(pub.get("fields"), list) and "lead_id" in pub.get("fields", []), "publish.fields must include lead_id", e)
    banned = {"phone", "name", "client_name", "email", "first_name", "last_name"}
    _need(not banned & set(pub.get("fields", [])), "publish.fields must not include phone or name fields", e)
    return e


def load_config(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    problems = validate_config(cfg)
    if problems:
        raise ConfigError("Invalid config:\n- " + "\n- ".join(problems))
    start = cfg["agent_match"]["ringcx_log_start"]
    if isinstance(start, str):
        cfg["agent_match"]["ringcx_log_start"] = dt.date.fromisoformat(start)
    return cfg
