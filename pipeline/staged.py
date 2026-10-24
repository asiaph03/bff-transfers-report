"""Staged ingestion: read the five sources one at a time and keep only what the report can still use.

Off by default: `ingest.staged` in config/report.yaml, or `--ingest staged` on the command line. The default
flow (pipeline.sheets.read_all, then calculate) is untouched; this module is an alternative with the same results
and a much lower peak memory, because only one source table is alive at a time.

Why the result is the same. Ringba alone decides the selected paid leads. After that the report only looks at RingCX
calls and lender records whose phone belongs to one of those leads (match_agents and check_business merge on the
leads' phones; match_closes joins prepare_lenders(phones=...), which already keeps only those records). So each
later source is reduced to those rows as soon as it is read, then freed. Counts, status inventories and Closed-tab
counts are taken while the table is in memory, and the same pipeline.validate checks then run in build.run on those
numbers, in the same order, so the report reads the same.

Fail-closed behaviour is unchanged: any read error raises as before, and nothing is published unless every check
passes. If too few paid leads exist, the remaining sources are still read (and discarded) so that the failing run
reports the same checks as the default flow would.
"""
from __future__ import annotations

import gc
from dataclasses import dataclass, field

import pandas as pd

from . import rules
from .sheets import read_source


@dataclass
class Staged:
    sizes: dict = field(default_factory=dict)             # rows read per source
    unknown: dict = field(default_factory=dict)           # lender statuses that are neither closing nor known (U8)
    closed_rows: dict = field(default_factory=dict)       # rows with a closed status, per lender
    closed_tab_rows: dict = field(default_factory=dict)   # rows on each lender's Closed tab
    leads: pd.DataFrame | None = None                     # selected paid leads, before agent matching
    diag: dict | None = None
    rule_error: str | None = None                         # set when the paid leads could not be selected
    gaps: list | None = None                              # months without Ringba calls inside the window
    ringcx: pd.DataFrame | None = None                    # prepared RingCX rows of the leads' phones only
    crm: pd.DataFrame | None = None                       # lender records of the leads' phones only


def unavailable_reason(closer_on: bool, uses_database: bool) -> str | None:
    """Why staged ingestion cannot be used for this run (the default flow is used instead), or None.

    Both cases need the complete tables: the closing-agent preview reads ownership fields of the matched lender
    records, and the RingCX database comparison compares the whole database table with the whole sheet."""
    if closer_on:
        return "the closing-agent preview needs the full lender tables"
    if uses_database:
        return "the RingCX database comparison needs the full RingCX tables"
    return None


def _join(pieces: list) -> pd.DataFrame:
    kept = [p for p in pieces if len(p)]
    return pd.concat(kept or pieces[:1], ignore_index=True)


def ingest(cfg: dict, source, extra_columns: dict | None = None, timings: dict | None = None) -> Staged:
    st = Staged()
    sources, order = cfg["sources"], cfg["lenders"]["order"]

    # 1. Ringba: the only source that decides which leads the report covers.
    df = read_source(cfg, "ringba", sources["ringba"], source, extra_columns, timings, st.closed_tab_rows)
    st.sizes["ringba"] = len(df)
    ringba = rules.prepare_ringba(df, cfg)
    del df
    try:
        st.leads, st.diag = rules.select_paid_leads(ringba, cfg)
        if cfg["validation"]["data_gaps"]["warn_months_without_calls"]:
            st.gaps = rules.months_without_calls(ringba, st.leads["call_time"].min(), st.leads["call_time"].max())
    except rules.RuleError as e:
        st.rule_error = str(e)
    del ringba
    phones = None if st.leads is None else set(st.leads["phone"])
    gc.collect()

    # 2. RingCX: only calls from the selected leads can matter.
    df = read_source(cfg, "ringcx", sources["ringcx"], source, extra_columns, timings, st.closed_tab_rows)
    st.sizes["ringcx"] = len(df)
    if phones is not None:
        ringcx = rules.prepare_ringcx(df, cfg)
        st.ringcx = ringcx[ringcx["phone"].isin(phones)]
        del ringcx
    del df
    gc.collect()

    # 3. Lenders, one at a time, in the configured order (which is also the precedence order).
    pieces = []
    for key in order:
        df = read_source(cfg, key, sources[key], source, extra_columns, timings, st.closed_tab_rows)
        st.sizes[key] = len(df)
        st.unknown.update(rules.unknown_statuses({key: df}, cfg, only=(key,)))
        st.closed_rows.update(rules.closed_status_rows({key: df}, cfg, only=(key,)))
        if phones is not None:
            pieces.append(rules.prepare_lenders({key: df}, cfg, phones=phones, only=(key,)))
        del df
        gc.collect()
    if phones is not None:
        st.crm = _join(pieces)
    return st
