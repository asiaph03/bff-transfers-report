"""Build and validate the dashboard site.

    python -m pipeline.build --check-config
    python -m pipeline.build --out out                       # reads Google Sheets (needs GOOGLE_APPLICATION_CREDENTIALS)
    python -m pipeline.build --out out --local-files map.json  # development: exported copies
    python -m pipeline.build --out out --preview             # preview: missing baseline is only a warning
    python -m pipeline.build --out out --first-release       # only for the first approved publication

Writes out/site/ (index.html, data.json, summary.json, vendor/) and out/validation.md.
If any check fails, out/site/ is removed and the exit code is 1, so nothing can be deployed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

from . import rules, validate
from .config import ConfigError, load_config
from .sheets import GoogleSheetsSource, LocalSource, SourceError, assert_no_secret, read_all

ROOT = Path(__file__).resolve().parent.parent


def vendor_ok(path: Path) -> bool:
    expected = (ROOT / "site" / "vendor" / "SHA256SUMS").read_text().split()[0]
    return hashlib.sha256(path.read_bytes()).hexdigest() == expected


def fetch_baseline(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/summary.json", timeout=30) as r:
            return json.loads(r.read().decode("utf-8")).get("totals")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return None


def run(args) -> int:
    out = Path(args.out)
    site = out / "site"
    if site.exists():
        shutil.rmtree(site)
    out.mkdir(parents=True, exist_ok=True)
    rep = validate.Report()
    facts: dict = {}
    started = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    facts["build started (UTC)"] = started.isoformat()

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        rep.check(False, "config is valid", str(e))
        return finish(rep, facts, out, None)
    rep.check(True, "config is valid")

    try:
        rcx = cfg["sources"]["ringcx"]
        pg_source = None
        if args.ringcx_backend == "postgres":
            # Opt-in comparison run: preview only, always alongside the sheet.
            rcx["backend"] = "postgres"
            rcx["postgres"]["compare_with_sheets"] = True
        if rcx.get("backend") == "postgres":
            from .pg import check_pg_config

            problems = check_pg_config(rcx.get("postgres"))
            if problems:
                rep.check(False, "PostgreSQL RingCX mapping is complete", "; ".join(problems))
                return finish(rep, facts, out, None)
            pgc = rcx["postgres"]
            if not args.preview and not pgc["approved_for_dashboard"]:
                rep.check(False, "PostgreSQL RingCX source approved for the dashboard",
                          "not approved: run with --preview to compare it with the sheet")
                return finish(rep, facts, out, None)
            if not (pgc["order_by_approved"] and pgc["approved_for_dashboard"]):
                pgc["compare_with_sheets"] = True   # until approved, every database run is checked against the sheet
            from .pg import PostgresRingcxSource

            pg_source = PostgresRingcxSource(rcx["postgres"], rcx["columns"], cfg["agent_match"]["ringcx_log_start"])
            facts["ringcx source"] = f"PostgreSQL {rcx['postgres']['table']} (read-only)"
        source = LocalSource(json.loads(Path(args.local_files).read_text())) if args.local_files else GoogleSheetsSource()
        facts["source"] = "local exported copies (development)" if args.local_files else "Google Sheets (read-only)"
        ca = cfg.get("closing_agent")
        closer_on = bool(ca) and (ca["source"] == "lender" or args.closer_preview)
        extra = rules.lender_closer_columns(cfg) if closer_on else None
        tables, closed_tab_rows = read_all(cfg, source, pg_source, extra_columns=extra)
        if pg_source is not None:
            facts.update({f"ringcx db {k.replace('_', ' ')}": v for k, v in pg_source.diag.items()})
    except SourceError as e:
        rep.check(False, "all sources readable", str(e))
        return finish(rep, facts, out, None)
    rep.check(True, "all five sources readable with required columns")
    validate.check_sources(rep, tables, cfg)
    validate.check_statuses(rep, rules.unknown_statuses(tables, cfg))
    if closed_tab_rows:
        validate.check_closed_tabs(rep, rules.closed_status_rows(tables, cfg), closed_tab_rows)

    ringba = rules.prepare_ringba(tables["ringba"], cfg)
    try:
        leads, diag = rules.select_paid_leads(ringba, cfg)
    except rules.RuleError as e:
        rep.check(False, "enough paid leads", str(e))
        return finish(rep, facts, out, None)
    facts.update({k.replace("_", " "): v for k, v in diag.items()})
    rep.check(diag["paid_rows_unparsed_time"] <= cfg["validation"]["max_unparsed_paid_call_times"],
              "paid calls have readable call times", f"{diag['paid_rows_unparsed_time']} unreadable")

    ringcx = rules.prepare_ringcx(tables["ringcx"], cfg)
    leads_before_match = leads
    leads = rules.match_agents(leads, ringcx, cfg)
    if pg_source is not None:
        validate.check_pg_quality(rep, pg_source.diag, ringcx, cfg)
        if "ringcx_sheet" in tables:
            sheet_rcx = rules.prepare_ringcx(tables["ringcx_sheet"], cfg)
            facts.update(validate.compare_ringcx(rep, ringcx, sheet_rcx, leads_before_match, cfg))
    crm = rules.prepare_lenders(tables, cfg)
    leads, excluded = rules.match_closes(leads, crm, cfg)
    ca = cfg.get("closing_agent")
    if ca:
        use = ca["source"] == "lender" or args.closer_preview
        lm = ca["lender_mapping"]
        # Only owner-approved names (name_map) are ever used, in a preview and in an approved run alike.
        nmap = lm["name_map"]
        leads = rules.assign_closers(leads, tables if use else None, cfg, use_lender=use, name_map=nmap)
        counts = rules.closer_counts(leads, cfg)
        label = "approved lender mapping" if ca["source"] == "lender" else ("lender fields, approved names, preview only" if use else "not enabled")
        facts[f"closing agent ({label})"] = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in counts.items())
        unknown = {k: v for k, v in counts.items() if not k.startswith("verified") and v}
        if ca["source"] == "lender" and unknown:
            rep.warn("closing agent unknown for " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in unknown.items())
                     + " closed deal(s)")
    validate.check_business(rep, leads, ringcx, excluded, cfg)

    if cfg["validation"]["data_gaps"]["warn_months_without_calls"]:
        gaps = rules.months_without_calls(ringba, leads["call_time"].min(), leads["call_time"].max())
        if gaps:
            rep.warn(f"Ringba has no calls in {', '.join(gaps)} (inside the reporting window)")

    rows = rules.to_public_rows(leads, cfg)
    if args.closer_preview and "closing_agent" in leads:
        write_closer_preview(out / "closing_agent_preview.md", leads, cfg)
    totals = rules.summarize(rows)
    validate.check_output(rep, rows, totals, cfg)
    baseline = fetch_baseline(cfg["publish"]["site_url"])
    if baseline is None and args.preview:
        rep.warn("no published baseline yet; this preview cannot be compared with a previous release")
    else:
        validate.check_baseline(rep, totals, baseline, cfg, args.first_release)
    facts["baseline"] = "none" if baseline is None else json.dumps({k: baseline.get(k) for k in ("closed", "active", "debt_enrolled", "payout")})

    refreshed = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    summary = {
        "refreshed_at": refreshed,
        "window": {"size": cfg["report"]["window_size"], "label": cfg["report"]["lead_label"]},
        "totals": totals,
        "warnings": [w for w in rep.warnings if "baseline" not in w],  # data notes shown on the page
        "stale_after_days": cfg["validation"]["stale_after_days"],
        "config_version": cfg["version"],
    }
    data_text = json.dumps({"refreshed_at": refreshed, "rows": rows}, separators=(",", ":"))
    summary_text = json.dumps(summary, indent=2)
    page = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
    validate.check_privacy(rep, {"index.html": page, "data.json": data_text, "summary.json": summary_text})
    vend = ROOT / "site" / "vendor" / "d3.v7.8.5.min.js"
    rep.check(vend.exists() and vendor_ok(vend), "chart library file matches its recorded checksum")
    for name, text in (("data.json", data_text), ("summary.json", summary_text), ("index.html", page)):
        assert_no_secret(text, name)

    facts.update({"leads": totals["leads"], "closed": totals["closed"], "active": totals["active"],
                  "debt enrolled": totals["debt_enrolled"], "payout": totals["payout"],
                  "paid calls from": totals["first_paid_call"], "paid calls to": totals["last_paid_call"]})
    if not rep.ok:
        return finish(rep, facts, out, None)
    site.mkdir(parents=True)
    (site / "vendor").mkdir()
    (site / "index.html").write_text(page, encoding="utf-8")
    shutil.copy2(vend, site / "vendor" / vend.name)
    (site / "data.json").write_text(data_text, encoding="utf-8")
    (site / "summary.json").write_text(summary_text, encoding="utf-8")
    (site / ".nojekyll").write_text("", encoding="utf-8")
    facts["refreshed_at"] = refreshed
    return finish(rep, facts, out, site)


def write_closer_preview(path: Path, leads, cfg: dict) -> None:
    """Preview-only review file: per-lender aggregate counts and agent-name values; no phones or deal rows.
    Written beside validation.md, outside site/, so it is never published."""
    lm = cfg["closing_agent"]["lender_mapping"]
    d = rules.closer_diagnostics(leads, cfg)
    total = rules.closer_counts(leads, cfg)
    lines = ["# Closing-agent preview (diagnostic only, not published)", "",
             "Source: the matched lender CRM record's ownership fields. RingCX and the SI tab are not used.",
             f"Lender mapping approved: {lm['lender_values_approved']}. Name map: "
             + ("approved" if cfg["closing_agent"]["source"] == "lender" else "approved names (name_map) + approved alias Ben Hall; preview only"), "",
             "Fields: " + "; ".join(f"{k}: {' + '.join(v)}" for k, v in lm["fields"].items()), "",
             "## All lenders", ""] + [f"- {k.replace('_', ' ')}: {v}" for k, v in total.items()]
    for k, v in d.items():
        lines += ["", f"## {cfg['lenders']['display_names'].get(k, k)}", ""]
        for name, n in v.items():
            if not name.endswith("_values"):
                lines.append(f"- {name.replace('_', ' ')}: {n}")
        for name in ("verified_values", "unmapped_values", "non_bff_values"):     # original lender values (agent names)
            if v[name]:
                lines.append(f"- {name.replace('_', ' ')} (as written in the lender sheet): "
                             + ", ".join(f"{n} ({x})" for n, x in v[name].items()))
    text = "\n".join(lines) + "\n"
    assert_no_secret(text, path.name)
    path.write_text(text, encoding="utf-8")


def finish(rep: validate.Report, facts: dict, out: Path, site: Path | None) -> int:
    text = rep.markdown("BFF Transfers refresh: validation report", facts)
    assert_no_secret(text, "validation.md")
    (out / "validation.md").write_text(text, encoding="utf-8")
    print(text)
    if not rep.ok and (out / "site").exists():
        shutil.rmtree(out / "site")
    return 0 if rep.ok and site is not None else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config" / "report.yaml"))
    ap.add_argument("--out", default=str(ROOT / "out"))
    ap.add_argument("--local-files", help="development only: JSON map of source key -> exported file")
    ap.add_argument("--first-release", action="store_true", help="first approved publication only")
    ap.add_argument("--preview", action="store_true", help="preview build: a missing baseline is a warning, not a failure")
    ap.add_argument("--ringcx-backend", choices=("config", "postgres"), default="config",
                    help="postgres: read RingCX from the database and compare it with the sheet (preview only)")
    ap.add_argument("--closer-preview", action="store_true",
                    help="also read the lender ownership fields and write per-lender closing-agent counts to out/closing_agent_preview.md (never published)")
    ap.add_argument("--check-config", action="store_true")
    args = ap.parse_args(argv)
    if args.check_config:
        try:
            load_config(args.config)
        except ConfigError as e:
            print(e)
            return 1
        print("config OK")
        return 0
    try:
        return run(args)
    except Exception as e:  # never leave a run without a report; never print data or secrets
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        if (out / "site").exists():
            shutil.rmtree(out / "site")
        rep = validate.Report()
        rep.check(False, "build completed", f"unexpected {type(e).__name__} in {e.__traceback__.tb_frame.f_code.co_filename.rsplit('/', 1)[-1] if e.__traceback__ else 'build'}")
        return finish(rep, {"build stopped (UTC)": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()}, out, None)


if __name__ == "__main__":
    sys.exit(main())
