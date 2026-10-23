"""Read the source worksheets.

GoogleSheetsSource reads with a service account and the read-only scope.
LocalSource reads exported .xlsx/.csv copies, for development on your own machine only.
Both return, per worksheet, a table of the requested columns in sheet order, plus a
`_row` column holding the sheet row number.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pandas as pd

READONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
API = "https://sheets.googleapis.com/v4/spreadsheets"


class SourceError(RuntimeError):
    """A worksheet could not be read, or lacks a required column."""


def _col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _q(ws: str) -> str:
    return "'" + ws.replace("'", "''") + "'"


def header_index(header: list, wanted: list[str], where: str) -> dict[str, int]:
    """Map each wanted header to its first column position; fail if absent or repeated."""
    clean = [("" if h is None else str(h)).strip() for h in header]
    out, missing, repeated = {}, [], []
    for w in wanted:
        hits = [i for i, h in enumerate(clean) if h == w]
        if not hits:
            missing.append(w)
        elif len(hits) > 1:
            repeated.append(w)
        else:
            out[w] = hits[0]
    if missing or repeated:
        msg = []
        if missing:
            msg.append(f"missing column(s) {missing}")
        if repeated:
            msg.append(f"column name(s) used more than once {repeated}")
        raise SourceError(f"{where}: " + "; ".join(msg))
    return out


class GoogleSheetsSource:
    def __init__(self, key_json: str | None = None):
        from google.oauth2 import service_account
        from google.auth.transport.requests import AuthorizedSession

        raw = key_json if key_json is not None else os.environ.get("GOOGLE_SA_KEY", "")
        if not raw.strip():
            raise SourceError("GOOGLE_SA_KEY is not set: add the service-account key as a GitHub secret")
        try:
            info = json.loads(raw)
        except json.JSONDecodeError:
            raise SourceError("GOOGLE_SA_KEY is not valid JSON") from None
        creds = service_account.Credentials.from_service_account_info(info, scopes=[READONLY_SCOPE])
        self.session = AuthorizedSession(creds)
        self.account = info.get("client_email", "(unknown service account)")

    def _get(self, url: str, params: dict) -> dict:
        r = self.session.get(url, params=params, timeout=120)
        if r.status_code == 403:
            raise SourceError(f"permission denied: share the spreadsheet with {self.account} as Viewer")
        if r.status_code == 404:
            raise SourceError("spreadsheet or worksheet not found")
        if r.status_code >= 400:
            raise SourceError(f"Google Sheets API error {r.status_code}")
        return r.json()

    def read(self, spreadsheet_id: str, worksheet: str, columns: list[str]) -> pd.DataFrame:
        where = f"{worksheet}"
        head = self._get(f"{API}/{spreadsheet_id}/values/{_q(worksheet)}!1:1", {"valueRenderOption": "UNFORMATTED_VALUE"})
        header = (head.get("values") or [[]])[0]
        idx = header_index(header, columns, where)
        ranges = [f"{_q(worksheet)}!{_col_letter(i)}2:{_col_letter(i)}" for i in idx.values()]
        body = self._get(
            f"{API}/{spreadsheet_id}/values:batchGet",
            {"ranges": ranges, "majorDimension": "COLUMNS", "valueRenderOption": "UNFORMATTED_VALUE", "dateTimeRenderOption": "SERIAL_NUMBER"},
        )
        cols = [((vr.get("values") or [[]])[0]) for vr in body.get("valueRanges", [])]
        n = max((len(c) for c in cols), default=0)
        data = {name: (c + [None] * (n - len(c))) for name, c in zip(idx.keys(), cols)}
        df = pd.DataFrame(data)
        df["_row"] = range(2, n + 2)
        return df

    def count_rows(self, spreadsheet_id: str, worksheet: str, has_header: bool) -> int:
        body = self._get(f"{API}/{spreadsheet_id}/values/{_q(worksheet)}!A:E", {"valueRenderOption": "UNFORMATTED_VALUE"})
        rows = body.get("values", [])
        if has_header:
            rows = rows[1:]
        return sum(1 for r in rows if any(v not in (None, "") for v in r[:5]))


class LocalSource:
    """Development only: reads exported copies listed in a JSON map {source_key: path}.

    Files hold real client data: keep them outside the repository.
    """

    def __init__(self, files: dict[str, str]):
        self.files = files

    def _frame(self, path: str, worksheet: str) -> list[list]:
        p = Path(path)
        if p.suffix.lower() == ".csv":
            d = pd.read_csv(p, dtype=str, keep_default_na=False, header=None)
            return d.values.tolist()
        import openpyxl

        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        if worksheet not in wb.sheetnames:
            raise SourceError(f"{worksheet}: worksheet not found in {p.name}")
        return [list(r) for r in wb[worksheet].iter_rows(values_only=True)]

    def read_key(self, key: str, worksheet: str, columns: list[str]) -> pd.DataFrame:
        if key not in self.files:
            raise SourceError(f"{key}: no local file configured")
        rows = self._frame(self.files[key], worksheet)
        if not rows:
            raise SourceError(f"{worksheet}: empty")
        idx = header_index(rows[0], columns, worksheet)
        body = rows[1:]
        while body and all(v in (None, "") for v in body[-1]):
            body.pop()
        data = {name: [(r[i] if i < len(r) else None) for r in body] for name, i in idx.items()}
        df = pd.DataFrame(data)
        df["_row"] = range(2, len(body) + 2)
        return df

    def count_rows_key(self, key: str, worksheet: str, has_header: bool) -> int:
        rows = self._frame(self.files[key + ":closed"] if key + ":closed" in self.files else self.files[key], worksheet)
        if has_header:
            rows = rows[1:]
        return sum(1 for r in rows if any(v not in (None, "") for v in list(r)[:5]))


def read_all(cfg: dict, source) -> tuple[dict[str, pd.DataFrame], dict[str, int]]:
    """Read every configured worksheet and each lender's Closed tab row count."""
    tables, closed_counts = {}, {}
    for key, s in cfg["sources"].items():
        cols = []
        for v in s["columns"].values():
            if v is None:
                continue
            cols.extend(v if isinstance(v, list) else [v])
        if isinstance(source, LocalSource):
            tables[key] = source.read_key(key, s["worksheet"], cols)
        else:
            tables[key] = source.read(s["spreadsheet_id"], s["worksheet"], cols)
        ct = s.get("closed_tab")
        if ct and cfg["validation"].get("closed_tab_cross_check"):
            if isinstance(source, LocalSource):
                closed_counts[key] = source.count_rows_key(key, ct["worksheet"], ct["has_header"])
            else:
                closed_counts[key] = source.count_rows(s["spreadsheet_id"], ct["worksheet"], ct["has_header"])
    return tables, closed_counts


_SECRETISH = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\"private_key\"\s*:")


def assert_no_secret(text: str, where: str) -> None:
    if _SECRETISH.search(text):
        raise SourceError(f"{where}: looks like it contains a private key; refusing to write it")
