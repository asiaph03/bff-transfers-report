"""Read-only RingCX source from the BFF PostgreSQL table public.ringcx_inbound.

Opt-in only: used when the run asks for it (`--ringcx-backend postgres`, preview only) or when
config sets sources.ringcx.backend: postgres after approval. The table is maintained outside this
project; this module never writes to it.

Safety: read-only session (default_transaction_read_only) in one REPEATABLE READ snapshot, a
statement timeout on every query, identifiers only from the validated config, values only as bound
parameters, a row cap, and a check that the rows read equal the snapshot count. Errors carry the
exception type and SQLSTATE only, never the connection string, host, credentials or row values.

The returned table uses the RingCX sheet's column names (sources.ringcx.columns) so every reporting
rule runs unchanged. `_row` is the position in the configured ORDER BY, which stands in for sheet row
order in the same-day agent tie-break (U2); that substitution must be shown to match before approval.
"""
from __future__ import annotations

import datetime as dt
import decimal
import os
import re
import time

import pandas as pd

from .sheets import SourceError

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
FIELDS = ("date", "result", "agent", "phone")


def check_pg_config(pg: dict) -> list[str]:
    """Problems with sources.ringcx.postgres; empty when the mapping is complete and safe."""
    e: list[str] = []
    if not isinstance(pg, dict):
        return ["sources.ringcx.postgres is missing"]
    if not (isinstance(pg.get("dsn_env"), str) and IDENT.match(pg["dsn_env"])):
        e.append("sources.ringcx.postgres.dsn_env must be an environment variable name")
    table = pg.get("table")
    if not (isinstance(table, str) and table.count(".") <= 1 and all(IDENT.match(p) for p in table.split("."))):
        e.append("sources.ringcx.postgres.table must be table or schema.table")
    cols = pg.get("columns") or {}
    for f in FIELDS + ("call_id",):
        if not (isinstance(cols.get(f), str) and IDENT.match(cols[f])):
            e.append(f"sources.ringcx.postgres.columns.{f} must be a confirmed column name")
    q = cols.get("queue")
    if q is not None and not (isinstance(q, str) and IDENT.match(q)):
        e.append("sources.ringcx.postgres.columns.queue must be a column name or null")
    if q is not None and not (isinstance(pg.get("queue_value"), str) and pg["queue_value"]):
        e.append("sources.ringcx.postgres.queue_value is required when columns.queue is set")
    if q is None and pg.get("all_rows_are_queue") is not True:
        e.append("sources.ringcx.postgres: set columns.queue, or all_rows_are_queue: true once confirmed")
    ob = pg.get("order_by")
    if not (isinstance(ob, list) and ob and all(isinstance(c, str) and IDENT.match(c) for c in ob)):
        e.append("sources.ringcx.postgres.order_by must be a list of columns (stands in for sheet order, U2)")
    for k in ("order_by_approved", "approved_for_dashboard"):
        if not isinstance(pg.get(k), bool):
            e.append(f"sources.ringcx.postgres.{k} must be true or false")
    tz = pg.get("timestamp_timezone")
    if tz is not None and not (isinstance(tz, str) and re.match(r"^[A-Za-z_]+(/[A-Za-z_+-]+)*$", tz)):
        e.append("sources.ringcx.postgres.timestamp_timezone must be an IANA name such as America/New_York, or null")
    for k, lo, hi in (("statement_timeout_s", 1, 600), ("max_rows", 1, 5_000_000)):
        v = pg.get(k)
        if not (isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi):
            e.append(f"sources.ringcx.postgres.{k} must be an integer {lo}-{hi}")
    return e


def build_query(pg: dict, sheet_cols: dict):
    """Return (count_sql, rows_sql) as psycopg sql objects. Values are bound as %(since)s / %(queue)s."""
    from psycopg import sql

    c = pg["columns"]
    table = sql.Identifier(*pg["table"].split("."))
    where = [sql.SQL("{} >= %(since)s").format(sql.Identifier(c["date"]))]
    if c.get("queue"):
        where.append(sql.SQL("{} = %(queue)s").format(sql.Identifier(c["queue"])))
    where_sql = sql.SQL(" AND ").join(where)
    picks = [sql.SQL("{} AS {}").format(sql.Identifier(c[f]), sql.Identifier(sheet_cols[f])) for f in FIELDS]
    picks.append(sql.SQL("{} AS {}").format(sql.Identifier(c["call_id"]), sql.Identifier("_call_id")))
    order = sql.SQL(", ").join(sql.Identifier(x) for x in pg["order_by"])
    # Rows with no date never pass the date bound, so they are counted separately (same queue filter).
    queue_only = sql.SQL("{} = %(queue)s").format(sql.Identifier(c["queue"])) if c.get("queue") else sql.SQL("TRUE")
    count_sql = sql.SQL("SELECT count(*) FILTER (WHERE {w}), count(*) FILTER (WHERE {d} IS NULL) FROM {t} WHERE {q}").format(
        w=where_sql, d=sql.Identifier(c["date"]), t=table, q=queue_only)
    rows_sql = sql.SQL("SELECT {p} FROM {t} WHERE {w} ORDER BY {o}").format(
        p=sql.SQL(", ").join(picks), t=table, w=where_sql, o=order)
    return count_sql, rows_sql


def _plain_phone(v):
    """Database numbers (numeric/bigint) as digit text, so 15551234567.0 does not gain a digit."""
    if isinstance(v, decimal.Decimal):
        return str(int(v)) if v == v.to_integral_value() else str(v)
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return v


def _local_date_time(v, tz: str | None):
    """Dates and naive timestamps pass through; zone-aware timestamps need the configured reporting zone."""
    if isinstance(v, dt.datetime) and v.tzinfo is not None:
        if not tz:
            raise SourceError("ringcx database: interaction dates carry a time zone; set "
                              "sources.ringcx.postgres.timestamp_timezone to the RingCX reporting zone")
        return pd.Timestamp(v).tz_convert(tz).tz_localize(None).to_pydatetime()
    return v


class PostgresRingcxSource:
    """One read-only, repeatable-read transaction: a count, then the rows, from the same snapshot."""

    CONNECT_TIMEOUT = 15

    def __init__(self, pg: dict, sheet_cols: dict, since, connect=None):
        self.pg, self.sheet_cols, self.since = pg, sheet_cols, since
        self._connect = connect
        self.diag: dict = {}

    def _open(self):
        dsn = os.environ.get(self.pg["dsn_env"], "")
        if not dsn.strip():
            raise SourceError(f"ringcx database: {self.pg['dsn_env']} is not set")
        connect = self._connect
        if connect is None:
            import psycopg

            connect = psycopg.connect
        ms = int(self.pg["statement_timeout_s"]) * 1000
        opts = f"-c statement_timeout={ms} -c default_transaction_read_only=on"
        if self.pg.get("timestamp_timezone"):   # validated IANA name: the date bound is applied in the reporting zone
            opts += f" -c TimeZone={self.pg['timestamp_timezone']}"
        return connect(dsn, connect_timeout=self.CONNECT_TIMEOUT, application_name="bff-dashboard-readonly", options=opts)

    def read(self) -> pd.DataFrame:
        import psycopg

        where = f"ringcx database ({self.pg['table']})"
        count_sql, rows_sql = build_query(self.pg, self.sheet_cols)
        params = {"since": self.since}
        if self.pg["columns"].get("queue"):
            params["queue"] = self.pg["queue_value"]
        t0 = time.monotonic()
        try:
            conn = self._open()
        except SourceError:
            raise
        except psycopg.Error as e:
            raise SourceError(f"{where}: could not connect ({type(e).__name__}, SQLSTATE {getattr(e, 'sqlstate', None) or '-'})") from None
        try:
            conn.read_only = True
            conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            with conn.transaction():
                n, undated = conn.execute(count_sql, params).fetchone()
                if n > self.pg["max_rows"]:
                    raise SourceError(f"{where}: {n} rows exceed max_rows {self.pg['max_rows']}; not reading")
                cur = conn.execute(rows_sql, params)
                names = [d.name for d in cur.description]
                rows = cur.fetchall()
        except SourceError:
            raise
        except psycopg.Error as e:
            raise SourceError(f"{where}: query failed ({type(e).__name__}, SQLSTATE {getattr(e, 'sqlstate', None) or '-'})") from None
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - closing must never hide the real error
                pass
        if len(rows) != n:
            raise SourceError(f"{where}: read {len(rows)} rows but the snapshot count was {n}; not using a partial read")
        expected = [self.sheet_cols[f] for f in FIELDS] + ["_call_id"]
        if names != expected:
            raise SourceError(f"{where}: unexpected result columns; not using it")
        df = pd.DataFrame(rows, columns=names)
        d, p = self.sheet_cols["date"], self.sheet_cols["phone"]
        df[d] = [_local_date_time(v, self.pg.get("timestamp_timezone")) for v in df[d]]
        df[p] = [_plain_phone(v) for v in df[p]]
        df["_row"] = range(2, len(df) + 2)
        self.diag = self._quality(df)
        self.diag["missing_date"] += int(undated or 0)   # undated rows in the queue, outside the date bound
        self.diag["seconds"] = round(time.monotonic() - t0, 1)
        print(f"  {where}: {n} rows in {self.diag['seconds']}s ({self.diag['first_date']} to {self.diag['last_date']})", flush=True)
        return df

    def _quality(self, df: pd.DataFrame) -> dict:
        """Aggregate data-quality facts (counts and dates only; no values)."""
        sc = self.sheet_cols
        blank = lambda s: s.isna() | (s.astype(str).str.strip() == "")  # noqa: E731
        ids = df["_call_id"]
        dates = pd.to_datetime(df[sc["date"]], errors="coerce")
        return {
            "rows": len(df),
            "first_date": f"{dates.min():%Y-%m-%d}" if dates.notna().any() else None,
            "last_date": f"{dates.max():%Y-%m-%d}" if dates.notna().any() else None,
            "missing_date": int(blank(df[sc["date"]]).sum() + (dates.isna() & ~blank(df[sc["date"]])).sum()),
            "missing_result": int(blank(df[sc["result"]]).sum()),
            "missing_agent": int(blank(df[sc["agent"]]).sum()),
            "missing_phone": int(blank(df[sc["phone"]]).sum()),
            "missing_call_ids": int(blank(ids).sum()),
            "repeated_call_ids": int(ids[~blank(ids)].duplicated().sum()),
        }
