"""Checks run before anything is published. Any failure stops the run."""
from __future__ import annotations

import json
import re

import pandas as pd

PHONE_LIKE = re.compile(r"(?<!\d)(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}(?!\d)")
EMAIL_LIKE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class Report:
    def __init__(self):
        self.failures: list[str] = []
        self.warnings: list[str] = []
        self.passed: list[str] = []

    def check(self, ok: bool, name: str, detail: str = "") -> bool:
        (self.passed if ok else self.failures).append(name + (f": {detail}" if detail else ""))
        return ok

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    @property
    def ok(self) -> bool:
        return not self.failures

    def markdown(self, title: str, facts: dict) -> str:
        lines = [f"# {title}", "", f"**Result:** {'PASSED' if self.ok else 'FAILED'}", ""]
        if facts:
            lines += ["## Run facts", ""] + [f"- {k}: {v}" for k, v in facts.items()] + [""]
        for head, items in (("Failures", self.failures), ("Warnings", self.warnings), ("Passed checks", self.passed)):
            lines += [f"## {head} ({len(items)})", ""] + ([f"- {x}" for x in items] or ["- none"]) + [""]
        return "\n".join(lines)


def check_sources(rep: Report, tables: dict, cfg: dict) -> None:
    for key, s in cfg["sources"].items():
        n = len(tables.get(key, []))
        rep.check(n >= s["min_rows"], f"{key}: at least {s['min_rows']} rows", f"{n} rows")


def check_business(rep: Report, leads: pd.DataFrame, ringcx: pd.DataFrame, excluded: pd.DataFrame, cfg: dict) -> None:
    n = cfg["report"]["window_size"]
    floor = cfg["paid_leads"]["min_payout_exclusive"]
    exact = cfg["report"]["allow_fewer_than_window"] or len(leads) == n
    rep.check(exact, f"selected exactly {n} paid leads", f"{len(leads)} selected")
    rep.check(bool((leads["payout"] > floor).all()), "every selected lead has a payout above the threshold")
    rep.check(leads["phone"].is_unique, "selected leads are unique by phone")
    rep.check(bool(leads["call_time"].notna().all()), "every selected lead has a paid-call time")
    # agent rule
    am = cfg["agent_match"]
    ans = leads[leads["answered"]]
    x = ringcx.merge(ans[["phone", "call_time"]], on="phone")
    ok_rows = x[(x["result"] == am["accepted_result"]) & (x["date"] >= x["call_time"].dt.normalize())]
    rep.check(set(ans["phone"]) <= set(ok_rows["phone"]), "every agent match is an accepted call on or after the paid date")
    labels = {am["label_before_log"], am["label_not_answered"]}
    rep.check(bool(leads.loc[~leads["answered"], "agent"].isin(labels).all()), "unanswered leads carry a no-agent label")
    # close rule
    c = leads[leads["closed"]]
    rep.check(bool((c["close_date"].dt.normalize() >= c["call_time"].dt.normalize()).all()), "every close is on or after its paid-call date")
    rep.check(c["phone"].is_unique, "each lead is counted at most once across lenders")
    inactive = set(cfg["lenders"]["inactive_statuses"])
    rep.check(bool((leads["active"] == (leads["closed"] & ~leads["close_status"].isin(inactive))).all()), "active = closed and not an excluded status")
    if len(excluded):
        rep.warn(f"{excluded['phone'].nunique()} lead(s) have a closed-status lender record dated before the paid call or undated; not counted")


def check_statuses(rep: Report, unknown: dict) -> None:
    rep.check(not unknown, "no unexpected lender statuses", "; ".join(f"{k}: {v}" for k, v in unknown.items()))


def check_closed_tabs(rep: Report, status_rows: dict, tab_rows: dict) -> None:
    for k, n in tab_rows.items():
        rep.check(status_rows.get(k) == n, f"{k}: closed-status rows match the lender's Closed tab", f"{status_rows.get(k)} vs {n}")


def check_output(rep: Report, rows: list[dict], summary: dict, cfg: dict) -> None:
    fields = cfg["publish"]["fields"]
    rep.check(all(list(r.keys()) == fields for r in rows), "every published row has exactly the approved fields")
    ids = [r["lead_id"] for r in rows]
    rep.check(len(ids) == len(set(ids)), "lead ids are unique")
    rep.check(summary["closed"] == sum(r["closed"] for r in rows) and summary["active"] <= summary["closed"], "totals reconcile with rows")
    rep.check(sum(summary["closes_by_lender"].values()) == summary["closed"], "closes by lender add up to total closes")
    rep.check(summary["answered"] <= summary["leads"] and summary["closed"] <= summary["in_lender_crm"], "funnel steps never exceed the step before")


def check_privacy(rep: Report, files: dict[str, str]) -> None:
    for name, text in files.items():
        body = text
        if name.endswith(".html"):
            body = re.sub(r"<script[^>]*src=[^>]*></script>", "", text)
        phones = PHONE_LIKE.findall(json.dumps(json.loads(text)) if name.endswith(".json") else body)
        rep.check(not phones, f"{name}: no phone-number-like values", f"{len(phones)} found" if phones else "")
        emails = EMAIL_LIKE.findall(body)
        rep.check(not emails, f"{name}: no email addresses", f"{len(emails)} found" if emails else "")
        rep.check("private_key" not in body and "BEGIN PRIVATE KEY" not in body, f"{name}: no credentials")


def check_baseline(rep: Report, summary: dict, baseline: dict | None, cfg: dict, first_release: bool) -> None:
    bl = cfg["validation"]["baseline"]
    if baseline is None:
        if bl["required"] and not first_release:
            rep.check(False, "baseline available", "no published summary.json found; the first publication needs first_release=true")
        else:
            rep.warn("no baseline to compare with (first release)")
        return
    if first_release:
        rep.check(False, "first_release used only once", "a published baseline already exists; run without first_release")
        return
    for m, t in bl["metrics"].items():
        old, new = baseline.get(m), summary.get(m)
        if old is None or new is None:
            rep.check(False, f"baseline metric {m} present")
            continue
        diff = abs(new - old)
        pct = diff / old if old else (0 if diff == 0 else float("inf"))
        within = diff <= t["max_abs_change"] or pct <= t["max_pct_change"]
        rep.check(within, f"{m} change within limits", f"{old} -> {new}")
