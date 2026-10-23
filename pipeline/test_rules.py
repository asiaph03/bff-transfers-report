"""Rule tests on made-up data only (no real phones, names or amounts)."""
from __future__ import annotations

import copy
from pathlib import Path

import pandas as pd
import pytest

from pipeline import rules, validate
from pipeline.config import ConfigError, load_config, validate_config
from pipeline.sheets import LocalSource, SourceError, header_index, read_all

CFG_PATH = Path(__file__).resolve().parent.parent / "config" / "report.yaml"


@pytest.fixture()
def cfg():
    c = load_config(CFG_PATH)
    c = copy.deepcopy(c)
    c["report"]["window_size"] = 3
    return c


def fake_phone(i: int) -> str:
    return f"555{i:07d}"  # 555-prefixed test numbers


def ringba(rows):
    return pd.DataFrame([
        {"_row": i + 2, "inboundCallId": cid, "callDtReadable": t, "inboundPhoneNumber": ph,
         "buyerConversionAmount": amt, "buyerHasConnected": True, "buyerHasConverted": True}
        for i, (cid, t, ph, amt) in enumerate(rows)])


def leads_from(cfg, rows):
    return rules.select_paid_leads(rules.prepare_ringba(ringba(rows), cfg), cfg)


# ---- phone ----
@pytest.mark.parametrize("raw,want", [("1-555-000-0001", "5550000001"), (15550000001.0, "5550000001"),
                                      ("(555) 000 0001", "5550000001"), ("12345", ""), (None, ""), ("", "")])
def test_norm_phone(raw, want):
    assert rules.norm_phone(raw) == want


# ---- paid leads ----
def test_only_paid_calls_qualify(cfg):
    rows = [("a", "2026-01-01 10:00", fake_phone(1), 0), ("b", "2026-01-02 10:00", fake_phone(2), 32),
            ("c", "2026-01-03 10:00", fake_phone(3), 35), ("d", "2026-01-04 10:00", fake_phone(4), 32), ("e", "2026-01-05", fake_phone(5), None)]
    leads, _ = leads_from(cfg, rows)
    assert set(leads["phone"]) == {fake_phone(2), fake_phone(3), fake_phone(4)}


def test_latest_paid_call_per_phone_then_newest(cfg):
    rows = [("a", "2026-01-01 10:00", fake_phone(1), 32), ("b", "2026-03-01 10:00", fake_phone(1), 32),
            ("c", "2026-02-01 10:00", fake_phone(2), 32), ("d", "2026-02-02 10:00", fake_phone(3), 32),
            ("e", "2026-01-15 10:00", fake_phone(4), 32)]
    leads, _ = leads_from(cfg, rows)
    assert list(leads["call_id"]) == ["b", "d", "c"]  # newest first; phone 1 keeps its March call
    assert leads["phone"].is_unique


def test_tie_on_time_breaks_by_call_id(cfg):
    rows = [("x2", "2026-01-01 10:00", fake_phone(1), 32), ("x1", "2026-01-01 10:00", fake_phone(2), 32),
            ("x3", "2026-01-01 10:00", fake_phone(3), 32), ("x0", "2026-01-01 10:00", fake_phone(4), 32)]
    leads, _ = leads_from(cfg, rows)
    assert list(leads["call_id"]) == ["x3", "x2", "x1"]


def test_fewer_than_window_fails(cfg):
    with pytest.raises(rules.RuleError):
        leads_from(cfg, [("a", "2026-01-01", fake_phone(1), 32), ("b", "2026-01-02", fake_phone(2), 32)])


def test_fewer_than_window_allowed_by_config(cfg):
    cfg["report"]["allow_fewer_than_window"] = True
    leads, _ = leads_from(cfg, [("a", "2026-01-01", fake_phone(1), 32)])
    assert len(leads) == 1


def test_unreadable_time_is_counted_and_dropped(cfg):
    rows = [("a", "not a date", fake_phone(1), 32)] + [(f"b{i}", f"2026-01-0{i+1}", fake_phone(i + 2), 32) for i in range(3)]
    leads, diag = leads_from(cfg, rows)
    assert diag["paid_rows_unparsed_time"] == 1 and fake_phone(1) not in set(leads["phone"])


# ---- agents ----
def rcx(rows):
    return pd.DataFrame([{"_row": i + 2, "Date": d, "Call Result": res, "Agent Full Name": a, "ANI": ph}
                         for i, (d, res, a, ph) in enumerate(rows)],
                        columns=["_row", "Date", "Call Result", "Agent Full Name", "ANI"])


def base_leads(cfg):
    rows = [("a", "2026-05-10 10:00", fake_phone(1), 32), ("b", "2026-05-11 10:00", fake_phone(2), 32), ("c", "2026-03-20 10:00", fake_phone(3), 32)]
    return leads_from(cfg, rows)[0]


def test_agent_first_accepted_on_or_after_paid_date_and_alias(cfg):
    L = base_leads(cfg)
    x = rules.prepare_ringcx(rcx([
        ("5/9/2026", "Inbound Accepted", "Early Agent", "1" + fake_phone(1)),      # before paid date: ignored
        ("5/10/2026", "Inbound Abandoned", "N/A", "1" + fake_phone(1)),            # not accepted: ignored
        ("5/10/2026", "Inbound Accepted", "Ben Hall", "1" + fake_phone(1)),        # alias
        ("5/10/2026", "Inbound Accepted", "Second Agent", "1" + fake_phone(1)),    # same day, later row
    ]), cfg)
    out = rules.match_agents(L, x, cfg).set_index("phone")
    assert out.loc[fake_phone(1), "agent"] == "Benjamin Hall"
    assert out.loc[fake_phone(2), "agent"] == cfg["agent_match"]["label_not_answered"]
    assert out.loc[fake_phone(3), "agent"] == cfg["agent_match"]["label_before_log"]


def test_only_ben_hall_alias_is_configured():
    c = load_config(CFG_PATH)
    assert c["agent_match"]["aliases"] == {"BEN HALL": "BENJAMIN HALL"}


# ---- lenders ----
def lender_tables(cit=(), cor=(), dbl=()):
    t = {
        "citizen": pd.DataFrame([{"_row": i + 2, "Phone": p, "Status Name": s, "Submitted Date": d, "Total Enrolled Debt": debt, "Assignees": "AGENT A"} for i, (p, s, d, debt) in enumerate(cit)],
                                columns=["_row", "Phone", "Status Name", "Submitted Date", "Total Enrolled Debt", "Assignees"]),
        "cordoba": pd.DataFrame([{"_row": i + 2, "Home Phone": h, "Cell Phone": c, "Status": s, "Submitted Date": d, "Enrolled Debt": debt, "Assigned To": "Agent B"} for i, (h, c, s, d, debt) in enumerate(cor)],
                                columns=["_row", "Home Phone", "Cell Phone", "Status", "Submitted Date", "Enrolled Debt", "Assigned To"]),
        "debtblue": pd.DataFrame([{"_row": i + 2, "Home Phone": p, "Stage": s, "Created": d, "Assigned To": "Agent C"} for i, (p, s, d) in enumerate(dbl)],
                                 columns=["_row", "Home Phone", "Stage", "Created", "Assigned To"]),
    }
    return t


def closes(cfg, tables):
    L = rules.match_agents(base_leads(cfg), rules.prepare_ringcx(rcx([]), cfg), cfg)
    return rules.match_closes(L, rules.prepare_lenders(tables, cfg), cfg)


def test_close_must_be_on_or_after_paid_date_calendar(cfg):
    t = lender_tables(cit=[(fake_phone(1), "Enrolled", "2026-05-09 23:00", 1000),       # day before: not counted
                           (fake_phone(2), "Enrolled", "2026-05-11 01:00", 2000)])      # same day, earlier hour: counted
    out, excluded = closes(cfg, t)
    o = out.set_index("phone")
    assert not o.loc[fake_phone(1), "closed"] and o.loc[fake_phone(2), "closed"]
    assert fake_phone(1) in set(excluded["phone"])


def test_earliest_lender_wins_and_lead_counted_once(cfg):
    t = lender_tables(cit=[(fake_phone(1), "Enrolled", "2026-05-20", 1000)],
                      cor=[("", fake_phone(1), "Waiting For First Payment", "2026-05-12", 3000)],
                      dbl=[(fake_phone(1), "Enrolled", "2026-05-15")])
    out, _ = closes(cfg, t)
    r = out.set_index("phone").loc[fake_phone(1)]
    assert r["close_lender_key"] == "cordoba" and r["debt_enrolled"] == 3000
    assert int(out["closed"].sum()) == 1


def test_cordoba_matches_home_or_cell(cfg):
    t = lender_tables(cor=[(fake_phone(9), fake_phone(2), "Active", "2026-05-12", 500)])
    out, _ = closes(cfg, t)
    assert out.set_index("phone").loc[fake_phone(2), "closed"]


def test_active_excludes_statuses_and_debtblue_has_no_debt(cfg):
    t = lender_tables(cit=[(fake_phone(1), "Cancelled", "2026-05-12", 100)],
                      dbl=[(fake_phone(2), "Dropped / Cancelled", "2026-05-12")])
    out, _ = closes(cfg, t)
    o = out.set_index("phone")
    assert o.loc[fake_phone(1), "closed"] and not o.loc[fake_phone(1), "active"]
    assert o.loc[fake_phone(2), "closed"] and not o.loc[fake_phone(2), "active"]
    assert pd.isna(o.loc[fake_phone(2), "debt_enrolled"])


def test_non_closing_status_is_not_a_close(cfg):
    t = lender_tables(cit=[(fake_phone(1), "Pending", "2026-05-12", 100)], dbl=[(fake_phone(2), "Contacted", "2026-05-12")])
    out, _ = closes(cfg, t)
    assert int(out["closed"].sum()) == 0 and int(out["in_lender_crm"].sum()) == 2


def test_unknown_status_is_flagged(cfg):
    t = lender_tables(cit=[(fake_phone(1), "Brand New Status", "2026-05-12", 100)])
    assert rules.unknown_statuses(t, cfg) == {"citizen": ["Brand New Status"]}
    rep = validate.Report()
    validate.check_statuses(rep, rules.unknown_statuses(t, cfg))
    assert not rep.ok


def test_undated_closed_record_is_excluded(cfg):
    t = lender_tables(cor=[(fake_phone(1), "", "Returned", None, 900)])
    out, excluded = closes(cfg, t)
    assert int(out["closed"].sum()) == 0 and len(excluded) == 1


# ---- output, privacy, baseline ----
def test_public_rows_have_only_approved_fields_and_no_phones(cfg):
    out, _ = closes(cfg, lender_tables(cit=[(fake_phone(2), "Enrolled", "2026-05-12", 2000)]))
    rows = rules.to_public_rows(out, cfg)
    assert all(list(r) == cfg["publish"]["fields"] for r in rows)
    import json
    text = json.dumps(rows)
    assert fake_phone(2) not in text
    rep = validate.Report()
    validate.check_privacy(rep, {"data.json": text})
    assert rep.ok


def test_privacy_check_catches_a_phone():
    rep = validate.Report()
    validate.check_privacy(rep, {"data.json": '{"x": "555-000-0001"}'})
    assert not rep.ok


def test_baseline_rules(cfg):
    s = {"closed": 20, "active": 15, "debt_enrolled": 400000.0, "payout": 10000.0, "answered": 290}
    rep = validate.Report(); validate.check_baseline(rep, s, None, cfg, first_release=False); assert not rep.ok
    rep = validate.Report(); validate.check_baseline(rep, s, None, cfg, first_release=True); assert rep.ok
    rep = validate.Report(); validate.check_baseline(rep, s, dict(s), cfg, first_release=True); assert not rep.ok
    rep = validate.Report(); validate.check_baseline(rep, s, dict(s, closed=19), cfg, first_release=False); assert rep.ok
    rep = validate.Report(); validate.check_baseline(rep, s, dict(s, closed=200), cfg, first_release=False); assert not rep.ok


# ---- config and sources ----
def test_shipped_config_is_valid():
    assert validate_config(load_config(CFG_PATH)) == []


def test_invalid_config_is_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(CFG_PATH.read_text().replace("window_size: 300", "window_size: -1"))
    with pytest.raises(ConfigError):
        load_config(bad)


def test_config_rejects_phone_in_public_fields(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(CFG_PATH.read_text().replace("    - lead_id\n", "    - lead_id\n    - phone\n"))
    with pytest.raises(ConfigError):
        load_config(bad)


def test_missing_column_fails():
    with pytest.raises(SourceError):
        header_index(["a", "b"], ["a", "c"], "Sheet")


def test_repeated_column_fails():
    with pytest.raises(SourceError):
        header_index(["a", "a"], ["a"], "Sheet")


def test_failed_source_retrieval(cfg, tmp_path):
    src = LocalSource({"ringba": str(tmp_path / "missing.csv")})
    with pytest.raises(Exception):
        read_all(cfg, src)
