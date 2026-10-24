"""Read-only RingCX source from the BFF PostgreSQL table (default: ringcx_inbound).

Only used when config sets sources.ringcx.backend: postgres. The database is never written to:
the session is read-only, every statement has a timeout, identifiers come only from the validated
config and values are passed as parameters. Errors are reported by type and SQLSTATE only, never with
the connection string, host, credentials or row values.

The returned table uses the same column names as the RingCX sheet (sources.ringcx.columns), so every
reporting rule runs unchanged. `_row` is the position in the configured, approved ORDER BY, which takes
the place of sheet row order for the same-day agent tie-break (U2).
"""
from __future__ import annotations

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
    if not (isinstance(table, str) and all(IDENT.match(p) for p in table.split(".")) and table.count(".") <= 1):
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
        e.append("sources.ringcx.postgres.order_by must be an approved list of columns (replaces sheet order, U2)")
    for k, lo, hi in (("statement_timeout_s", 1, 600), ("max_rows", 1, 5_000_000)):
        v = pg.get(k)
        if not (isinstance(v, int) and lo <= v <= hi):
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
    count_sql = sql.SQL(
        "SELECT count(*), count(DISTINCT {cid}), min({d}), max({d}) FROM {t} WHERE {w}"
    ).format(cid=sql.Identifier(c["call_id"]), d=sql.Identifier(c["date"]), t=table, w=where_sql)
    rows_sql = sql.SQL("SELECT {p} FROM {t} WHERE {w} ORDER BY {o}").format(
        p=sql.SQL(", ").join(picks), t=table, w=where_sql, o=order)
    return count_sql, rows_sql


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
        return connect(dsn, connect_timeout=self.CONNECT_TIMEOUT, application_name="bff-dashboard-readonly",
                       options=f"-c statement_timeout={ms} -c default_transaction_read_only=on")

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
                n, n_ids, dmin, dmax = conn.execute(count_sql, params).fetchone()
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
        df = pd.DataFrame(rows, columns=names)
        for f in FIELDS:
            if self.sheet_cols[f] not in df.columns:
                raise SourceError(f"{where}: result is missing {self.sheet_cols[f]}")
        df["_row"] = range(2, len(df) + 2)
        self.diag = {"rows": n, "duplicate_call_ids": int(n - n_ids), "first_date": str(dmin)[:10] if dmin else None,
                     "last_date": str(dmax)[:10] if dmax else None, "seconds": round(time.monotonic() - t0, 1)}
        print(f"  {where}: {n} rows in {self.diag['seconds']}s ({self.diag['first_date']} to {self.diag['last_date']})", flush=True)
        return df
