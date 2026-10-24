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


# ---- PostgreSQL RingCX source: fake connection only (no database, no real data) ----
from pipeline import pg as pgmod  # noqa: E402

SHEET_COLS = {"date": "Date", "result": "Call Result", "agent": "Agent Full Name", "phone": "ANI"}


def pg_cfg(**over):
    c = {"dsn_env": "RINGCX_DATABASE_URL", "table": "ringcx_inbound",
         "columns": {"date": "call_date", "result": "call_result", "agent": "agent_name", "phone": "ani",
                     "call_id": "uii", "queue": "queue"},
         "queue_value": "Two-Minute Transfers", "all_rows_are_queue": False, "order_by": ["call_start", "uii"],
         "statement_timeout_s": 60, "max_rows": 1000, "compare_with_sheets": False}
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
    """Answers the count query, then the rows query; records SQL, params and connection options."""

    def __init__(self, rows, count=None, ids=None, fail_on=None):
        self.rows, self.count, self.ids, self.fail_on = rows, count, ids, fail_on
        self.sql, self.params, self.closed, self.read_only = [], [], False, None

    def transaction(self):
        import contextlib

        return contextlib.nullcontext()

    def execute(self, q, params):
        import psycopg

        text = q.as_string(None)
        self.sql.append(text)
        self.params.append(params)
        if self.fail_on and self.fail_on in text:
            raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout (secret host db.example)")
        if text.startswith("SELECT count("):
            n = len(self.rows) if self.count is None else self.count
            ids = n if self.ids is None else self.ids
            dates = [r[0] for r in self.rows] or [None]
            return _Cur([(n, ids, min(dates) if self.rows else None, max(dates) if self.rows else None)], ["c"])
        return _Cur(self.rows, ["Date", "Call Result", "Agent Full Name", "ANI", "_call_id"])

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


DB_ROWS = [(_dt.datetime(2026, 5, 10, 9, 5), "Inbound Accepted", "First Agent", "1" + fake_phone(1), "U1"),
           (_dt.datetime(2026, 5, 10, 9, 40), "Inbound Accepted", "Second Agent", "1" + fake_phone(1), "U2")]


def test_pg_config_requires_confirmed_mapping_and_order():
    import yaml

    shipped = yaml.safe_load(CFG_PATH.read_text())["sources"]["ringcx"]
    assert shipped["backend"] == "sheets"                          # nothing switched yet
    problems = pgmod.check_pg_config(shipped["postgres"])
    assert any("columns.date" in p for p in problems) and any("order_by" in p for p in problems)
    assert pgmod.check_pg_config(pg_cfg()) == []
    bad = pg_cfg(columns={**pg_cfg()["columns"], "date": "call_date; DROP TABLE x"})
    assert any("columns.date" in p for p in pgmod.check_pg_config(bad))
    assert any("table" in p for p in pgmod.check_pg_config(pg_cfg(table="a.b.c")))


def test_pg_query_is_read_only_bounded_parameterised_and_ordered(monkeypatch):
    conn = FakeConn(DB_ROWS)
    src, opened = pg_source(conn, monkeypatch)
    df = src.read()
    assert "default_transaction_read_only=on" in opened["options"] and "statement_timeout=60000" in opened["options"]
    assert conn.read_only is True and conn.closed
    rows_sql = conn.sql[1]
    assert rows_sql.startswith('SELECT "call_date" AS "Date"') and 'ORDER BY "call_start", "uii"' in rows_sql
    assert "%(since)s" in rows_sql and "%(queue)s" in rows_sql and "Two-Minute" not in rows_sql   # values are parameters
    assert conn.params[1] == {"since": _dt.date(2026, 4, 1), "queue": "Two-Minute Transfers"}
    assert not any(w in " ".join(conn.sql).upper() for w in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "ALTER", "CREATE", "DROP"))
    assert list(df["Agent Full Name"]) == ["First Agent", "Second Agent"] and list(df["_row"]) == [2, 3]


def test_pg_rows_drive_the_same_rules_and_tie_break(cfg, monkeypatch):
    src, _ = pg_source(FakeConn(DB_ROWS), monkeypatch)
    rcx_db = rules.prepare_ringcx(src.read(), cfg)
    L = leads_from(cfg, [("a", "2026-05-10 08:00", fake_phone(1), 32), ("b", "2026-05-11 10:00", fake_phone(2), 32),
                         ("c", "2026-05-12 10:00", fake_phone(3), 32)])[0]
    out = rules.match_agents(L, rcx_db, cfg).set_index("phone")
    assert out.loc[fake_phone(1), "agent"] == "First Agent"         # first in the approved order wins
    assert out.loc[fake_phone(2), "agent"] == cfg["agent_match"]["label_not_answered"]


def test_pg_empty_result_is_valid_and_caught_by_min_rows(cfg, monkeypatch):
    src, _ = pg_source(FakeConn([]), monkeypatch)
    df = src.read()
    assert len(df) == 0 and {"Date", "ANI"} <= set(df.columns)
    rep = validate.Report()
    validate.check_sources(rep, {"ringcx": df}, {"sources": {"ringcx": {"min_rows": 500}}})
    assert not rep.ok


def test_pg_duplicate_ids_are_reported(monkeypatch):
    src, _ = pg_source(FakeConn(DB_ROWS, ids=1), monkeypatch)
    src.read()
    assert src.diag["duplicate_call_ids"] == 1


def test_pg_partial_read_and_row_cap_fail(monkeypatch):
    src, _ = pg_source(FakeConn(DB_ROWS, count=3), monkeypatch)
    with pytest.raises(SourceError, match="partial read"):
        src.read()
    src, _ = pg_source(FakeConn(DB_ROWS, count=5000), monkeypatch)
    with pytest.raises(SourceError, match="exceed max_rows"):
        src.read()


def test_pg_connection_and_query_failures_are_sanitised(monkeypatch):
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


def test_compare_ringcx_reports_counts_only(cfg):
    base = pd.DataFrame({"_row": [2, 3, 4], "date": pd.to_datetime(["2026-05-10"] * 3),
                         "result": ["Inbound Accepted"] * 3, "agent": ["A", "B", "C"],
                         "phone": [fake_phone(1), fake_phone(1), fake_phone(2)]})
    L = leads_from(cfg, [("a", "2026-05-10 08:00", fake_phone(1), 32), ("b", "2026-05-10 09:00", fake_phone(2), 32),
                         ("c", "2026-05-12 10:00", fake_phone(3), 32)])[0]
    same = rules.match_agents(L, base, cfg)
    rep = validate.Report()
    facts = validate.compare_ringcx(rep, base, base, same, same, cfg)
    assert facts["calls only in database / only in sheet"] == "0 / 0" and not rep.warnings
    flipped = base.assign(_row=[3, 2, 4])                            # database orders the tie the other way
    rep = validate.Report()
    facts = validate.compare_ringcx(rep, flipped, base, rules.match_agents(L, flipped, cfg), same, cfg)
    assert facts["same-day multi-agent ties decided differently"] == "1 of 1"
    assert facts["leads with different answered / agent"] == "0 / 1"
    assert all(fake_phone(1) not in str(v) for v in facts.values())
