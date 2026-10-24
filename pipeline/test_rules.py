"""Rule tests on made-up data only (no real phones, names or amounts)."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np
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


def test_unexpected_error_still_writes_report(tmp_path, monkeypatch):
    from pipeline import build

    def explode(args):
        raise RuntimeError("boom")

    monkeypatch.setattr(build, "run", explode)
    rc = build.main(["--out", str(tmp_path)])
    assert rc == 1 and "FAILED" in (tmp_path / "validation.md").read_text()


def test_serial_time_rounds_float_error_to_the_second():
    # 2026-10-19 16:33:48 as a spreadsheet serial number carries float error just below :48
    serial = 46314 + (16 * 3600 + 33 * 60 + 48) / 86400 - 1e-11
    assert rules.parse_time(serial).strftime("%Y-%m-%dT%H:%M:%S") == "2026-10-19T16:33:48"


# ---- Google Sheets reads: mocked HTTP only (no network, no real data) ----
import datetime as _dt  # noqa: E402

import requests  # noqa: E402

from pipeline import sheets  # noqa: E402

TAB = "Two Minute Transfers"
RCX_HEADER = ["Date", "Call Result", "Agent Full Name", "ANI", "Unused"]


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


class Resp:
    def __init__(self, status=200, body=None, headers=None, bad_json=False):
        self.status_code, self._body, self.headers, self._bad = status, body if body is not None else {}, headers or {}, bad_json

    def json(self):
        if self._bad:
            raise ValueError("truncated")
        return self._body


class ScriptedSession:
    """Plays back responses (or raises exceptions) in order; records each call."""

    def __init__(self, script, clock=None, took=0.0):
        self.script, self.calls, self.clock, self.took = list(script), [], clock, took

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        if self.clock:
            self.clock.t += self.took
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def unavailable():
    return Resp(503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "The service is currently unavailable."}})


def rcx_body(rows):
    return {"range": f"'{TAB}'!A1:E{len(rows) + 1}", "majorDimension": "ROWS", "values": [RCX_HEADER] + rows}


def make_source(script, took=0.0):
    clk = FakeClock()
    sess = ScriptedSession(script, clk, took)
    src = sheets.GoogleSheetsSource(session=sess, clock=clk.now, sleep=clk.sleep, rand=lambda: 0.5)
    src.start_source()
    return src, sess, clk


RCX_ROWS = [[46153.0, "Inbound Accepted", "First Agent", "1" + fake_phone(1), "x"],
            [46153.0, "Inbound Accepted", "Second Agent", "1" + fake_phone(1)]]   # short row: trailing cells omitted


def test_503_then_success_reads_the_whole_tab_in_one_request():
    src, sess, clk = make_source([unavailable(), Resp(200, rcx_body(RCX_ROWS))])
    df = src.read("sheet-id", TAB, ["Date", "Call Result", "Agent Full Name", "ANI"], whole_tab=True)
    assert len(sess.calls) == 2 and all("!" not in c[0] for c in sess.calls)   # whole tab, no range split
    assert len(clk.slept) == 1 and list(df["_row"]) == [2, 3]
    assert sess.calls[0][2] == sheets.GoogleSheetsSource.TIMEOUT


def test_several_transient_errors_then_success_with_growing_jittered_waits():
    script = [Resp(429, headers={"Retry-After": "30"}), Resp(500), Resp(502), Resp(504), Resp(200, rcx_body(RCX_ROWS))]
    src, sess, clk = make_source(script)
    src.read("sheet-id", TAB, ["Date", "ANI"], whole_tab=True)
    assert len(sess.calls) == 5
    assert clk.slept[0] >= 30                       # Retry-After respected
    assert clk.slept[1] < clk.slept[2] < clk.slept[3] <= sheets.GoogleSheetsSource.BACKOFF_CAP


def test_quota_403_is_retried_but_permission_403_is_not():
    quota = Resp(403, {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "Quota exceeded",
                                 "errors": [{"reason": "rateLimitExceeded"}]}})
    src, sess, _ = make_source([quota, Resp(200, rcx_body(RCX_ROWS))])
    src.read("sheet-id", TAB, ["ANI"], whole_tab=True)
    assert len(sess.calls) == 2
    denied = Resp(403, {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "The caller does not have permission"}})
    src, sess, _ = make_source([denied, Resp(200, rcx_body(RCX_ROWS))])
    with pytest.raises(SourceError, match="permission denied"):
        src.read("sheet-id", TAB, ["ANI"], whole_tab=True)
    assert len(sess.calls) == 1


def test_persistent_503_stops_at_the_source_time_budget():
    # each 503 takes 185 s, as observed in the GitHub runs
    src, sess, clk = make_source([unavailable() for _ in range(10)], took=185)
    with pytest.raises(SourceError, match="time budget ran out.*HTTP 503 UNAVAILABLE"):
        src.read("sheet-id", TAB, ["ANI"], whole_tab=True)
    assert clk.t <= sheets.GoogleSheetsSource.SOURCE_BUDGET
    assert 1 < len(sess.calls) < sheets.GoogleSheetsSource.MAX_ATTEMPTS


def test_persistent_503_stops_at_max_attempts_when_errors_are_fast():
    src, sess, _ = make_source([unavailable() for _ in range(10)])
    with pytest.raises(SourceError, match="still unavailable after 6 attempts"):
        src.read("sheet-id", TAB, ["ANI"], whole_tab=True)
    assert len(sess.calls) == 6


def test_request_timeout_is_retried_then_succeeds():
    src, sess, _ = make_source([requests.exceptions.ReadTimeout("slow"), requests.exceptions.ConnectionError("reset"),
                                Resp(200, rcx_body(RCX_ROWS))])
    df = src.read("sheet-id", TAB, ["ANI"], whole_tab=True)
    assert len(sess.calls) == 3 and len(df) == 2


@pytest.mark.parametrize("status,msg", [(401, "refused the sign-in"), (404, "not found"), (400, "API error 400")])
def test_permanent_errors_fail_at_once(status, msg):
    src, sess, _ = make_source([Resp(status), Resp(200, rcx_body(RCX_ROWS))])
    with pytest.raises(SourceError, match=msg):
        src.read("sheet-id", TAB, ["ANI"], whole_tab=True)
    assert len(sess.calls) == 1


def test_empty_tab_is_valid_but_a_missing_or_partial_response_is_not():
    src, _, _ = make_source([Resp(200, rcx_body([]))])
    assert len(src.read("sheet-id", TAB, ["ANI"], whole_tab=True)) == 0        # header only: zero rows, read succeeded
    src, _, _ = make_source([Resp(200, {"range": f"'{TAB}'!A1:Z1000"})])
    with pytest.raises(SourceError, match="no header row"):
        src.read("sheet-id", TAB, ["ANI"], whole_tab=True)
    src, _, _ = make_source([Resp(200, {"range": "'Other'!A1:E3", "values": [RCX_HEADER]})])
    with pytest.raises(SourceError, match="different range"):
        src.read("sheet-id", TAB, ["ANI"], whole_tab=True)
    src, _, _ = make_source([Resp(200, bad_json=True), Resp(200, rcx_body(RCX_ROWS))])   # unreadable body is retried
    assert len(src.read("sheet-id", TAB, ["ANI"], whole_tab=True)) == 2
    header = Resp(200, {"values": [["Date", "ANI"]]})
    partial = Resp(200, {"valueRanges": [{"values": [[1, 2]]}]})                         # 1 of 2 columns
    src, _, _ = make_source([header, partial])
    with pytest.raises(SourceError, match="1 of 2 requested columns"):
        src.read("sheet-id", "Ringba2", ["Date", "ANI"])


def test_ringcx_row_order_is_kept_for_the_agent_tie_break(cfg):
    src, _, _ = make_source([Resp(200, rcx_body(RCX_ROWS))])
    df = src.read("sheet-id", TAB, ["Date", "Call Result", "Agent Full Name", "ANI"], whole_tab=True)
    assert list(df["Agent Full Name"]) == ["First Agent", "Second Agent"] and list(df["_row"]) == [2, 3]
    L = leads_from(cfg, [("a", _dt.datetime(2026, 5, 10, 9), fake_phone(1), 32), ("b", "2026-05-11 10:00", fake_phone(2), 32),
                         ("c", "2026-05-12 10:00", fake_phone(3), 32)])[0]
    out = rules.match_agents(L, rules.prepare_ringcx(df, cfg), cfg).set_index("phone")
    assert out.loc[fake_phone(1), "agent"] == "First Agent"      # same day: first row in sheet order wins


class _FakeCreds:
    """google-auth Credentials stand-in: counts refreshes, can start expired."""

    def __new__(cls, *a, **k):
        from google.auth import credentials as gc

        class C(gc.Credentials):
            def __init__(self, expired_first):
                super().__init__()
                self.refreshes = 0
                self.token = "old-token" if not expired_first else None
                self.expiry = (_dt.datetime.utcnow() - _dt.timedelta(minutes=1)) if expired_first else None

            def refresh(self, request):
                self.refreshes += 1
                self.token = f"token-{self.refreshes}"
                self.expiry = _dt.datetime.utcnow() + _dt.timedelta(hours=1)

        return C(*a, **k)


class _Adapter(requests.adapters.HTTPAdapter):
    """Answers like Google: 401 for 'old-token', then the scripted responses; records Authorization headers."""

    def __init__(self, script):
        super().__init__()
        self.script, self.auth = list(script), []

    def send(self, request, **kw):
        self.auth.append(request.headers.get("authorization"))
        status, body = (401, {"error": {"code": 401, "status": "UNAUTHENTICATED"}}) if request.headers.get("authorization") == "Bearer old-token" else self.script.pop(0)
        r = requests.models.Response()
        r.status_code, r._content, r.request, r.url = status, __import__("json").dumps(body).encode(), request, request.url
        r.headers["Content-Type"] = "application/json"
        return r


def _authorized_source(creds, adapter):
    from google.auth.transport.requests import AuthorizedSession

    clk = FakeClock()
    sess = AuthorizedSession(creds, max_refresh_attempts=1)
    sess.mount("https://", adapter)
    src = sheets.GoogleSheetsSource(session=sess, clock=clk.now, sleep=clk.sleep, rand=lambda: 0.5)
    src.start_source()
    return src


def test_expired_token_is_refreshed_before_the_request():
    creds, ad = _FakeCreds(True), _Adapter([(200, rcx_body(RCX_ROWS))])
    df = _authorized_source(creds, ad).read("sheet-id", TAB, ["ANI"], whole_tab=True)
    assert creds.refreshes == 1 and ad.auth == ["Bearer token-1"] and len(df) == 2


def test_token_rejected_mid_run_is_refreshed_and_the_read_retried():
    creds, ad = _FakeCreds(False), _Adapter([(200, rcx_body(RCX_ROWS))])
    df = _authorized_source(creds, ad).read("sheet-id", TAB, ["ANI"], whole_tab=True)
    assert creds.refreshes == 1 and ad.auth == ["Bearer old-token", "Bearer token-1"] and len(df) == 2


def test_401_after_a_refresh_is_permanent():
    creds, ad = _FakeCreds(True), _Adapter([(401, {"error": {"code": 401}}), (401, {"error": {"code": 401}}), (200, {})])
    with pytest.raises(SourceError, match="refused the sign-in"):
        _authorized_source(creds, ad).read("sheet-id", TAB, ["ANI"], whole_tab=True)


def test_missing_credentials_give_a_clear_error(monkeypatch):
    import google.auth
    from google.auth.exceptions import DefaultCredentialsError

    def none(*a, **k):
        raise DefaultCredentialsError("none")

    monkeypatch.setattr(google.auth, "default", none)
    with pytest.raises(SourceError, match="credentials not found"):
        sheets.GoogleSheetsSource()


def test_error_log_masks_long_numbers_and_never_prints_tokens(capsys):
    body = {"error": {"code": 503, "status": "UNAVAILABLE", "message": "row 5550100001 unavailable"}}
    src, _, _ = make_source([Resp(503, body), Resp(200, rcx_body(RCX_ROWS))])
    src.read("sheet-id", TAB, ["ANI"], whole_tab=True)
    out = capsys.readouterr().out
    assert "5550100001" not in out and "row # unavailable" in out and "Bearer" not in out and "HTTP 503" in out


# ---- PostgreSQL RingCX source: fake connections only (no database, no real data) ----
import decimal  # noqa: E402

from pipeline import pg as pgmod  # noqa: E402

SHEET_COLS = {"date": "Date", "result": "Call Result", "agent": "Agent Full Name", "phone": "ANI"}
RESULT_COLS = ["Date", "Call Result", "Agent Full Name", "ANI", "_call_id"]


def pg_cfg(**over):
    c = load_config(CFG_PATH)["sources"]["ringcx"]["postgres"]
    c = copy.deepcopy(c)
    c.update(max_rows=1000)
    c.update(over)
    return c


class _Cur:
    def __init__(self, rows, names):
        self._rows = rows
        self.description = [type("D", (), {"name": n})() for n in names]

    def fetchone(self):
        return self._rows[0]

    def fetchall(self):
        return self._rows


class FakeConn:
    """Answers the count query, then the rows query; records SQL, params and connection settings."""

    def __init__(self, rows, count=None, fail_on=None, names=RESULT_COLS, undated=0, seq_missing=0, seq_repeated=0):
        self.rows, self.count, self.fail_on, self.names, self.undated = rows, count, fail_on, names, undated
        self.seq_missing, self.seq_repeated = seq_missing, seq_repeated
        self.sql, self.params, self.closed, self.read_only, self.isolation_level = [], [], False, None, None

    def transaction(self):
        import contextlib

        return contextlib.nullcontext()

    def execute(self, q, params):
        import psycopg

        text = q.as_string(None)
        self.sql.append(text)
        self.params.append(params)
        if self.fail_on and self.fail_on in text:
            raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout (host db.example)")
        if text.startswith("SELECT count("):
            n = len(self.rows) if self.count is None else self.count
            distinct = n - self.seq_missing - self.seq_repeated
            return _Cur([(n, self.undated, self.seq_missing, distinct)], ["count", "undated", "seq_missing", "seq_distinct"])
        return _Cur(self.rows, self.names)

    def close(self):
        self.closed = True


def pg_source(conn, monkeypatch, cfg=None, env="postgresql://user:secret@db.example/bff"):
    monkeypatch.setenv("RINGCX_DATABASE_URL", env)
    opened = {}

    def connect(dsn, **kw):
        opened.update(kw)
        return conn

    src = pgmod.PostgresRingcxSource(cfg or pg_cfg(), SHEET_COLS, _dt.date(2026, 4, 1), connect=connect)
    return src, opened


def row(day, result, agent, phone_n, uii, hour=9):
    return (_dt.datetime(2026, 5, day, hour), result, agent, "1" + fake_phone(phone_n), uii)


DB_ROWS = [row(10, "Inbound Accepted", "First Agent", 1, "U1"), row(10, "Inbound Accepted", "Second Agent", 1, "U2", 10)]


def test_shipped_pg_mapping_is_complete_but_not_switched_on():
    c = load_config(CFG_PATH)["sources"]["ringcx"]
    assert c["backend"] == "sheets"
    pgc = c["postgres"]
    assert pgc["table"] == "public.ringcx_inbound" and pgc["queue_value"] == "Two-Minute Transfers"
    assert pgc["columns"] == {"date": "interaction_date", "result": "call_result", "agent": "agent_full_name",
                              "phone": "ani", "call_id": "uii", "queue": "queue"}
    assert pgc["order_by_approved"] is False and pgc["approved_for_dashboard"] is False
    assert pgc["order_by"] == ["interaction_date", "source_seq"] and pgc["sequence_column"] == "source_seq"
    assert pgmod.check_pg_config(pgc) == []


def test_pg_config_rejects_unsafe_or_incomplete_values():
    bad = pg_cfg(columns={**pg_cfg()["columns"], "date": "interaction_date; DROP TABLE x"})
    assert any("columns.date" in p for p in pgmod.check_pg_config(bad))
    assert any("table" in p for p in pgmod.check_pg_config(pg_cfg(table="a.b.c")))
    assert any("order_by" in p for p in pgmod.check_pg_config(pg_cfg(order_by=None)))
    assert any("approved_for_dashboard" in p for p in pgmod.check_pg_config(pg_cfg(approved_for_dashboard="yes")))
    assert any("timestamp_timezone" in p for p in pgmod.check_pg_config(pg_cfg(timestamp_timezone="x; y")))


def test_pg_query_is_read_only_bounded_parameterised_and_ordered(monkeypatch):
    conn = FakeConn(DB_ROWS)
    src, opened = pg_source(conn, monkeypatch)
    df = src.read()
    assert "default_transaction_read_only=on" in opened["options"] and "statement_timeout=60000" in opened["options"]
    assert opened["connect_timeout"] == 15 and conn.read_only is True and conn.closed
    assert "TimeZone" not in opened["options"]
    _, opened_tz = pg_source(FakeConn(DB_ROWS), monkeypatch, cfg=pg_cfg(timestamp_timezone="America/New_York"))
    _.read()
    assert "-c TimeZone=America/New_York" in opened_tz["options"]
    import psycopg

    assert conn.isolation_level == psycopg.IsolationLevel.REPEATABLE_READ
    rows_sql = conn.sql[1]
    assert rows_sql.startswith('SELECT "interaction_date" AS "Date", "call_result" AS "Call Result", '
                               '"agent_full_name" AS "Agent Full Name", "ani" AS "ANI", "uii" AS "_call_id"')
    assert 'FROM "public"."ringcx_inbound"' in rows_sql
    assert 'ORDER BY "interaction_date", "source_seq"' in rows_sql
    assert '"interaction_date" >= %(since)s AND "queue" = %(queue)s' in rows_sql and "Two-Minute" not in rows_sql
    assert conn.params[1] == {"since": _dt.date(2026, 4, 1), "queue": "Two-Minute Transfers"}
    assert not any(w in " ".join(conn.sql).upper() for w in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "ALTER", "CREATE", "DROP", "GRANT"))
    assert list(df["Agent Full Name"]) == ["First Agent", "Second Agent"] and list(df["_row"]) == [2, 3]


def test_pg_numeric_phone_and_dates_normalise_like_the_sheet(cfg, monkeypatch):
    rows = [(_dt.date(2026, 5, 10), "Inbound Accepted", "Ben Hall", decimal.Decimal("15550000001"), "U1"),
            (_dt.datetime(2026, 5, 11, 23, 30), "Inbound Accepted", "A", 15550000002.0, "U2")]
    src, _ = pg_source(FakeConn(rows), monkeypatch)
    rcx = rules.prepare_ringcx(src.read(), cfg)
    assert list(rcx["phone"]) == ["5550000001", "5550000002"]           # no extra digit from numeric types
    assert list(rcx["date"].dt.strftime("%Y-%m-%d")) == ["2026-05-10", "2026-05-11"]
    assert rcx["agent"].iloc[0] == "BENJAMIN HALL"                        # the one approved alias still applies


def test_pg_zone_aware_dates_need_the_reporting_zone(cfg, monkeypatch):
    utc = _dt.timezone.utc
    rows = [(_dt.datetime(2026, 5, 11, 2, 30, tzinfo=utc), "Inbound Accepted", "A", "15550000001", "U1")]
    src, _ = pg_source(FakeConn(rows), monkeypatch)
    with pytest.raises(SourceError, match="timestamp_timezone"):
        src.read()
    src, _ = pg_source(FakeConn(rows), monkeypatch, cfg=pg_cfg(timestamp_timezone="America/New_York"))
    rcx = rules.prepare_ringcx(src.read(), cfg)
    assert rcx["date"].iloc[0] == pd.Timestamp("2026-05-10")              # 02:30 UTC is the previous New York day


def test_pg_rows_drive_the_same_rules_and_tie_break(cfg, monkeypatch):
    src, _ = pg_source(FakeConn(DB_ROWS), monkeypatch)
    rcx_db = rules.prepare_ringcx(src.read(), cfg)
    L = leads_from(cfg, [("a", "2026-05-10 08:00", fake_phone(1), 32), ("b", "2026-05-11 10:00", fake_phone(2), 32),
                         ("c", "2026-03-12 10:00", fake_phone(3), 32)])[0]
    out = rules.match_agents(L, rcx_db, cfg).set_index("phone")
    assert out.loc[fake_phone(1), "agent"] == "First Agent"               # first in query order wins the same-day tie
    assert out.loc[fake_phone(2), "agent"] == cfg["agent_match"]["label_not_answered"]
    assert out.loc[fake_phone(3), "agent"] == cfg["agent_match"]["label_before_log"]


def test_pg_empty_result_is_read_and_then_fails_min_rows(cfg, monkeypatch):
    src, _ = pg_source(FakeConn([]), monkeypatch)
    df = src.read()
    assert len(df) == 0 and src.diag["rows"] == 0 and src.diag["first_date"] is None
    rep = validate.Report()
    validate.check_sources(rep, {"ringcx": df}, {"sources": {"ringcx": {"min_rows": 500}}})
    assert not rep.ok


def test_pg_null_and_repeated_ids_are_counted_and_warned(cfg, monkeypatch):
    rows = DB_ROWS + [row(11, "Inbound Abandoned", "N/A", 2, "U2"), row(12, "Inbound Abandoned", "N/A", 3, None)]
    src, _ = pg_source(FakeConn(rows), monkeypatch)
    df = src.read()
    assert src.diag["repeated_call_ids"] == 1 and src.diag["missing_call_ids"] == 1
    rep = validate.Report()
    validate.check_pg_quality(rep, src.diag, rules.prepare_ringcx(df, cfg), cfg)
    assert rep.ok and len(rep.warnings) == 2


def test_pg_missing_required_values_fail_closed(cfg, monkeypatch):
    cases = [
        [(None, "Inbound Accepted", "A", "15550000001", "U1")],               # no date
        [(_dt.date(2026, 5, 10), None, "A", "15550000001", "U1")],            # no result
        [(_dt.date(2026, 5, 10), "Inbound Accepted", None, "15550000001", "U1")],  # accepted call, no agent
        [(_dt.date(2026, 5, 10), "Inbound Accepted", "A", "", "U1")],         # accepted call, no phone
        [("not a date", "Inbound Accepted", "A", "15550000001", "U1")],       # malformed date
    ]
    for rows in cases:
        src, _ = pg_source(FakeConn(rows), monkeypatch)
        df = src.read()
        rep = validate.Report()
        validate.check_pg_quality(rep, src.diag, rules.prepare_ringcx(df, cfg), cfg)
        assert not rep.ok, rows
    ok = [(_dt.date(2026, 5, 10), "Inbound Abandoned", None, "15550000001", "U1")]   # abandoned call without agent is fine
    src, _ = pg_source(FakeConn(ok), monkeypatch)
    df = src.read()
    rep = validate.Report()
    validate.check_pg_quality(rep, src.diag, rules.prepare_ringcx(df, cfg), cfg)
    assert rep.ok


def test_pg_partial_malformed_and_oversized_reads_fail(monkeypatch):
    src, _ = pg_source(FakeConn(DB_ROWS, count=3), monkeypatch)
    with pytest.raises(SourceError, match="partial read"):
        src.read()
    src, _ = pg_source(FakeConn(DB_ROWS, count=5000), monkeypatch)
    with pytest.raises(SourceError, match="exceed max_rows"):
        src.read()
    src, _ = pg_source(FakeConn(DB_ROWS, names=["Date", "Call Result", "Agent", "ANI", "_call_id"]), monkeypatch)
    with pytest.raises(SourceError, match="unexpected result columns"):
        src.read()


def test_pg_timeout_and_connection_failures_are_sanitised(monkeypatch):
    import psycopg

    monkeypatch.delenv("RINGCX_DATABASE_URL", raising=False)
    src = pgmod.PostgresRingcxSource(pg_cfg(), SHEET_COLS, _dt.date(2026, 4, 1), connect=lambda *a, **k: None)
    with pytest.raises(SourceError, match="RINGCX_DATABASE_URL is not set"):
        src.read()

    def refuse(dsn, **kw):
        raise psycopg.OperationalError("connection to server at db.example failed: password authentication failed for user bob")

    monkeypatch.setenv("RINGCX_DATABASE_URL", "postgresql://bob:secret@db.example/bff")
    src = pgmod.PostgresRingcxSource(pg_cfg(), SHEET_COLS, _dt.date(2026, 4, 1), connect=refuse)
    with pytest.raises(SourceError) as e:
        src.read()
    assert "could not connect" in str(e.value) and "secret" not in str(e.value) and "db.example" not in str(e.value)

    conn = FakeConn(DB_ROWS, fail_on="ORDER BY")
    src, _ = pg_source(conn, monkeypatch)
    with pytest.raises(SourceError) as e:
        src.read()
    assert "query failed (QueryCanceled" in str(e.value) and "db.example" not in str(e.value) and conn.closed


def _prepared(rows):
    return pd.DataFrame({"_row": range(2, len(rows) + 2), "date": pd.to_datetime([r[0] for r in rows]),
                         "result": [r[1] for r in rows], "agent": [r[2] for r in rows], "phone": [r[3] for r in rows]})


CMP = [("2026-05-10", "Inbound Accepted", "A", fake_phone(1)), ("2026-05-10", "Inbound Accepted", "B", fake_phone(1)),
       ("2026-05-10", "Inbound Abandoned", "N/A", fake_phone(2)), ("2026-05-11", "Inbound Accepted", "C", fake_phone(2))]


def _leads(cfg):
    return leads_from(cfg, [("a", "2026-05-10 08:00", fake_phone(1), 32), ("b", "2026-05-10 09:00", fake_phone(2), 32),
                            ("c", "2026-05-12 10:00", fake_phone(3), 32)])[0]


def test_compare_identical_sources_passes_with_counts_only(cfg):
    rep = validate.Report()
    facts = validate.compare_ringcx(rep, _prepared(CMP), _prepared(CMP), _leads(cfg), cfg)
    assert rep.ok and not rep.warnings
    assert facts["ringcx rows database / sheet"] == "4 / 4" and facts["leads with different answered / agent"] == "0 / 0"
    assert all(fake_phone(1) not in str(v) and "A" != v for v in facts.values())


def test_compare_order_difference_fails(cfg):
    flipped = _prepared(CMP).assign(_row=[3, 2, 4, 5])                    # database settles the A/B tie the other way
    rep = validate.Report()
    facts = validate.compare_ringcx(rep, flipped, _prepared(CMP), _leads(cfg), cfg)
    assert not rep.ok and facts["same-day multi-agent ties (database order / sheet order differ)"] == "1 of 1"
    assert any("agent attribution" in f for f in rep.failures)


def test_compare_missing_call_fails_but_later_snapshot_rows_only_warn(cfg):
    rep = validate.Report()
    validate.compare_ringcx(rep, _prepared(CMP[:2] + CMP[3:]), _prepared(CMP), _leads(cfg), cfg)   # earlier-day call missing
    assert any("call for call" in f for f in rep.failures) and any("call-result totals" in f for f in rep.failures)
    rep = validate.Report()
    validate.compare_ringcx(rep, _prepared(CMP[:3]), _prepared(CMP[:2] + CMP[3:]), _leads(cfg), cfg)
    assert any("only adds calls" in f for f in rep.failures)        # the later snapshot lacks a last-shared-day call
    later = CMP + [("2026-05-13", "Inbound Accepted", "D", fake_phone(3))]   # database loaded a day later
    rep = validate.Report()
    facts = validate.compare_ringcx(rep, _prepared(later), _prepared(CMP), _leads(cfg), cfg)
    assert rep.ok and facts["rows after the compared day database / sheet (snapshot timing)"] == "1 / 0"
    assert any("snapshots differ in timing" in w for w in rep.warnings)


def test_compare_partial_last_day_only_warns_but_conflicting_last_day_fails(cfg):
    partial = _prepared(CMP[:3])                         # sheet loaded mid-day: 2026-05-11 call not there yet
    more = _prepared(CMP + [("2026-05-11", "Inbound Abandoned", "N/A", fake_phone(3))])
    sheet_only_day = _prepared(CMP[:3] + [("2026-05-11", "Inbound Abandoned", "N/A", fake_phone(3))])
    rep = validate.Report()
    validate.compare_ringcx(rep, _prepared(CMP), partial, _leads(cfg), cfg)          # last shared day 05-10 identical
    assert rep.ok
    rep = validate.Report()
    validate.compare_ringcx(rep, more, _prepared(CMP), _leads(cfg), cfg)              # database has extra on last day
    assert rep.ok and any("snapshots differ in timing" in w for w in rep.warnings)
    rep = validate.Report()
    validate.compare_ringcx(rep, _prepared(CMP), sheet_only_day, _leads(cfg), cfg)    # each has a call the other lacks
    assert any("only adds calls" in f for f in rep.failures)


def test_postgres_cannot_publish_until_approved(tmp_path):
    from pipeline import build

    rc = build.main(["--out", str(tmp_path), "--ringcx-backend", "postgres"])     # no --preview
    assert rc == 1 and not (tmp_path / "site").exists()
    assert "approved for the dashboard" in (tmp_path / "validation.md").read_text()


def test_failed_postgres_read_writes_no_site(tmp_path, monkeypatch):
    from pipeline import build

    monkeypatch.delenv("RINGCX_DATABASE_URL", raising=False)
    monkeypatch.setattr(build, "GoogleSheetsSource", lambda: object())
    monkeypatch.setattr(build, "read_all", lambda cfg, source, pg, **kw: (pg.read(), {}))
    rc = build.main(["--out", str(tmp_path), "--ringcx-backend", "postgres", "--preview"])
    text = (tmp_path / "validation.md").read_text()
    assert rc == 1 and not (tmp_path / "site").exists()
    assert "RINGCX_DATABASE_URL is not set" in text and "FAILED" in text


def test_pg_undated_rows_outside_the_date_bound_still_fail(cfg, monkeypatch):
    conn = FakeConn(DB_ROWS, undated=2)                  # two queue rows with no interaction_date
    src, _ = pg_source(conn, monkeypatch)
    df = src.read()
    count_sql = conn.sql[0]
    assert 'count(*) FILTER (WHERE "interaction_date" IS NULL)' in count_sql and 'WHERE "queue" = %(queue)s' in count_sql
    assert src.diag["missing_date"] == 2
    rep = validate.Report()
    validate.check_pg_quality(rep, src.diag, rules.prepare_ringcx(df, cfg), cfg)
    assert any("interaction date" in f for f in rep.failures)


# ---- ingestion sequence (simulated only) ----
def test_pg_config_refuses_unstable_system_columns_as_order():
    for col in ("ctid", "CTID", "xmin"):
        assert any("system columns" in p for p in pgmod.check_pg_config(pg_cfg(order_by=["interaction_date", col])))
    assert any("real column" in p for p in pgmod.check_pg_config(pg_cfg(sequence_column="ctid")))
    assert any("part of order_by" in p for p in pgmod.check_pg_config(pg_cfg(order_by=["interaction_date", "uii"])))


def test_pg_sequence_is_counted_in_the_same_snapshot(monkeypatch):
    conn = FakeConn(DB_ROWS)
    src, _ = pg_source(conn, monkeypatch)
    src.read()
    assert '"source_seq" IS NULL' in conn.sql[0] and 'count(DISTINCT "source_seq")' in conn.sql[0]
    assert src.diag["missing_sequence"] == 0 and src.diag["repeated_sequence"] == 0


def test_pg_missing_or_repeated_sequence_fails_closed(cfg, monkeypatch):
    for kw in ({"seq_missing": 1}, {"seq_repeated": 1}):
        src, _ = pg_source(FakeConn(DB_ROWS, **kw), monkeypatch)
        df = src.read()
        rep = validate.Report()
        validate.check_pg_quality(rep, src.diag, rules.prepare_ringcx(df, cfg), cfg)
        assert any("ingestion sequence" in f for f in rep.failures), kw


def test_pg_without_sequence_column_skips_that_check(cfg, monkeypatch):
    src, _ = pg_source(FakeConn(DB_ROWS), monkeypatch, cfg=pg_cfg(sequence_column=None, order_by=["interaction_date", "uii"]))
    df = src.read()
    assert "missing_sequence" not in src.diag
    rep = validate.Report()
    validate.check_pg_quality(rep, src.diag, rules.prepare_ringcx(df, cfg), cfg)
    assert rep.ok


# ---- closing agent, separate from the answering agent (simulated data only) ----
def _closed_leads(lenders, agents=None):
    """Closed leads whose matched lender record is row 2, 3, ... of that lender's table (last one not closed)."""
    n = len(lenders)
    return pd.DataFrame({"phone": [fake_phone(i + 1) for i in range(n)],
                         "agent": agents or [f"Answer {i}" for i in range(n)],
                         "closed": [x is not None for x in lenders],
                         "close_lender_key": lenders,
                         "close_row": [i + 2 if x else np.nan for i, x in enumerate(lenders)]})


def _tables(**recs):
    """Fake lender tables: lender -> list of {column: value} records, rows numbered from 2 in lead order."""
    out = {}
    for k, rows in recs.items():
        df = pd.DataFrame(rows)
        df["_row"] = [r.pop("_row") if "_row" in r else None for r in rows] if any("_row" in r for r in rows) else range(2, len(rows) + 2)
        out[k] = df
    return out


NAMES = {"BENJAMIN HALL": "Benjamin Hall", "JAY MUDRA": "Jay Mudra", "Ian Rawley": "Ian Rawley"}


def _assign(cfg, leads, tables, name_map=NAMES):
    return rules.assign_closers(leads, tables, cfg, use_lender=True, name_map=name_map)


def test_shipped_config_keeps_lender_closer_in_preview():
    c = load_config(CFG_PATH)
    ca = c["closing_agent"]
    assert ca["source"] is None and ca["lender_mapping"]["name_map"] is None
    assert ca["lender_mapping"]["lender_values_approved"] is False
    assert ca["lender_mapping"]["fields"] == {"citizen": ["Created By", "Assignees"], "cordoba": ["Assigned To"],
                                              "debtblue": ["Assigned To"]}
    assert "closing_agent" not in c["publish"]["fields"]
    assert not c["sources"]["ringcx"].get("postgres", {}).get("approved_for_dashboard")


def test_closer_config_guards():
    c = load_config(CFG_PATH)
    bad = copy.deepcopy(c); bad["closing_agent"]["source"] = "lender"            # nothing approved yet
    assert any("approved lender_values and an approved name_map" in p for p in validate_config(bad))
    bad = copy.deepcopy(c); bad["closing_agent"]["source"] = "candidate"         # SI ledger is no longer a source
    assert any("must be null or lender" in p for p in validate_config(bad))
    bad = copy.deepcopy(c); bad["publish"]["fields"] = bad["publish"]["fields"] + ["closing_agent"]
    assert any("closing_agent only once" in p for p in validate_config(bad))
    bad = copy.deepcopy(c); del bad["closing_agent"]["lender_mapping"]["fields"]["debtblue"]
    assert any("lender_mapping.fields" in p for p in validate_config(bad))


def test_not_enabled_means_every_closed_deal_is_closer_unknown(cfg):
    L = _closed_leads(["cordoba", "citizen", None])
    out = rules.assign_closers(L, None, cfg, use_lender=False)
    assert list(out["closer_status"]) == ["no_source", "no_source", "not_closed"]
    assert list(out["closing_agent"][:2]) == ["Closer unknown"] * 2 and out["closing_agent"][2] is None
    assert list(out["agent"]) == list(L["agent"])                                   # answering agent untouched


def test_known_closer_from_each_lender_and_difference_from_answering_agent(cfg):
    L = _closed_leads(["citizen", "cordoba", "debtblue", None], agents=["Ben Hall", "Max Fromm", "X", "Y"])
    T = _tables(citizen=[{"Created By": "BENJAMIN HALL", "Assignees": "benjamin hall"}],
                cordoba=[{"_row": 3, "Assigned To": "Ian Rawley"}],
                debtblue=[{"_row": 4, "Assigned To": "Jay Mudra", "Assigned Company": "Better Future Finance"}])
    out = _assign(cfg, L, T)
    assert list(out["closing_agent"][:3]) == ["Benjamin Hall", "Ian Rawley", "Jay Mudra"]
    assert list(out["closer_status"]) == ["verified", "verified", "verified", "not_closed"]
    counts = rules.closer_counts(out, cfg)
    assert counts["verified"] == 3 and counts["verified_differs_from_answering_agent"] == 2   # Ben Hall = Benjamin Hall


def test_missing_and_blank_ownership_never_fall_back(cfg):
    L = _closed_leads(["citizen", "cordoba"], agents=["Ana Answer", "Bo Answer"])
    T = _tables(citizen=[{"Created By": "", "Assignees": float("nan")}],
                cordoba=[{"_row": 3, "Assigned To": "nan"}])
    out = _assign(cfg, L, T)
    assert list(out["closer_status"]) == ["missing", "missing"]
    assert set(out["closing_agent"]) == {"Closer unknown"}


def test_system_accounts_and_lender_staff_are_not_bff(cfg):
    L = _closed_leads(["citizen", "cordoba", "debtblue", "debtblue"])
    T = _tables(citizen=[{"Created By": "BFF API", "Assignees": "JAY MUDRA"}],
                cordoba=[{"_row": 3, "Assigned To": "Jacob Aparecio"}],
                debtblue=[{"_row": 4, "Assigned To": "Mayra Vazquez", "Assigned Company": "Debt Blue Servicing"},
                          {"_row": 5, "Assigned To": "Jay Mudra", "Assigned Company": "Council Law Group"}])
    out = _assign(cfg, L, T)
    assert list(out["closer_status"]) == ["non_bff"] * 4
    assert set(out["closing_agent"]) == {"Closer unknown"}
    d = rules.closer_diagnostics(out, cfg)
    assert d["citizen"]["other_field_would_resolve"] == 1 and d["debtblue"]["system_or_non_bff"] == 2


def test_conflicting_created_by_and_assignee_is_unknown(cfg):
    L = _closed_leads(["citizen", "citizen"])
    T = _tables(citizen=[{"Created By": "JAY MUDRA", "Assignees": "BENJAMIN HALL"},
                         {"Created By": "JAY MUDRA", "Assignees": ""}])
    out = _assign(cfg, L, T)
    assert list(out["closer_status"]) == ["conflicting", "partial"]
    assert set(out["closing_agent"]) == {"Closer unknown"}
    assert rules.closer_diagnostics(out, cfg)["citizen"]["conflicting_fields"] == 1


def test_unmapped_names_stay_unknown_and_no_alias_is_implied(cfg):
    L = _closed_leads(["citizen", "cordoba"])
    T = _tables(citizen=[{"Created By": "CAM HYDER", "Assignees": "CAM HYDER"}],
                cordoba=[{"_row": 3, "Assigned To": "Camaran Hyder"}])
    out = _assign(cfg, L, T)                                                         # neither name is in the map
    assert list(out["closer_status"]) == ["unmapped", "unmapped"]
    assert rules.closer_diagnostics(out, cfg)["citizen"]["unmapped_values"] == {"CAM HYDER": 1}
    out = _assign(cfg, L, T, name_map=None)                                          # no approved map at all
    assert list(out["closer_status"]) == ["unmapped", "unmapped"]


def test_closer_uses_the_matched_lender_record_only(cfg):
    L = _closed_leads(["cordoba"])
    T = _tables(cordoba=[{"_row": 2, "Assigned To": "Ian Rawley"}, {"_row": 9, "Assigned To": "Jay Mudra"}],
                citizen=[{"_row": 2, "Created By": "JAY MUDRA", "Assignees": "JAY MUDRA"}])
    assert _assign(cfg, L, T)["closing_agent"][0] == "Ian Rawley"


def test_closer_preview_reads_ownership_columns_only_when_asked(cfg, tmp_path):
    assert rules.lender_closer_columns(cfg) == {"citizen": ["Created By", "Assignees"], "cordoba": ["Assigned To"],
                                                "debtblue": ["Assigned To", "Assigned Company"]}


def test_closer_diagnostics_hold_no_phone_numbers(cfg):
    L = _closed_leads(["cordoba"])
    out = _assign(cfg, L, _tables(cordoba=[{"Assigned To": "Ian Rawley"}]))
    assert fake_phone(1) not in json.dumps(rules.closer_diagnostics(out, cfg))


def test_published_rows_do_not_gain_closing_agent(cfg):
    L = leads_from(cfg, [("a", "2026-05-10 10:00", fake_phone(1), 32), ("b", "2026-05-11 10:00", fake_phone(2), 32),
                         ("c", "2026-05-12 10:00", fake_phone(3), 32)])[0]
    L = rules.match_agents(L, rules.prepare_ringcx(rcx([]), cfg), cfg)
    crm = rules.prepare_lenders({k: pd.DataFrame({**{col: [] for col in (cfg["sources"][k]["columns"]["phones"]
                                  + [cfg["sources"][k]["columns"][x] for x in ("status", "close_date", "agent")]
                                  + ([cfg["sources"][k]["columns"]["debt"]] if cfg["sources"][k]["columns"]["debt"] else []))},
                                  "_row": []}) for k in ("citizen", "cordoba", "debtblue")}, cfg)
    L, _ = rules.match_closes(L, crm, cfg)
    L = rules.assign_closers(L, None, cfg, use_lender=False)
    rows = rules.to_public_rows(L, cfg)
    assert all(list(r) == cfg["publish"]["fields"] for r in rows) and all("closing_agent" not in r for r in rows)
