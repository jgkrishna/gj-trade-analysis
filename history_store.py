"""
SQLite-backed persistent log of detected reversals.
=======================================================

Every time the dashboard (or check_alerts.py) detects reversals, it calls
record_reversals(), which inserts any that aren't already stored (deduped
on ticker + confirmed-on date + direction) and returns just the newly
inserted rows -- that return value is what the alerting feature uses to
decide "is this actually new since last time," and what the dashboard's
history log displays cumulatively across ticker/threshold changes.

--------------------------------------------------------------------------
CAVEAT -- read this before relying on it in production
--------------------------------------------------------------------------
On most PaaS free/starter web services (Render's Starter plan included,
without an add-on), the filesystem is EPHEMERAL: it resets on every
redeploy or restart. This module works correctly wherever it's run, but
for the history to actually survive across restarts in production you
need either a Render persistent disk (mounted at a path, then point
HISTORY_DB_PATH at it) or an external database. Locally, this just works
with no extra setup. See DEPLOY.md.
"""

from __future__ import annotations

import datetime
import math
import os
import sqlite3
from pathlib import Path

import pandas as pd

DB_PATH = os.environ.get("HISTORY_DB_PATH") or str(Path(__file__).parent / "data" / "reversal_history.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS detected_reversals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    pivot_date TEXT NOT NULL,
    confirmed_on TEXT NOT NULL,
    prior_direction TEXT NOT NULL,
    new_direction TEXT NOT NULL,
    pct_move REAL,
    confidence INTEGER NOT NULL,
    methods TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    UNIQUE(ticker, confirmed_on, new_direction)
)
"""


def _connect() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(_SCHEMA)
    return conn


def record_reversals(ticker: str, df, reversals) -> list[dict]:
    """Insert any reversals not already stored for this ticker. Returns
    only the rows that were newly inserted (genuinely new since the last
    call) -- reruns on unchanged data return an empty list."""
    conn = _connect()
    new_rows: list[dict] = []
    try:
        for r in reversals:
            pct_move = None if math.isnan(r.pct_move) else float(r.pct_move)
            row = dict(
                ticker=ticker,
                pivot_date=r.date.date().isoformat(),
                confirmed_on=df.index[r.confirm_idx].date().isoformat(),
                prior_direction=r.prior_direction,
                new_direction=r.new_direction,
                pct_move=pct_move,
                confidence=r.confidence,
                methods="+".join(r.sources),
            )
            cur = conn.execute(
                "INSERT OR IGNORE INTO detected_reversals "
                "(ticker, pivot_date, confirmed_on, prior_direction, new_direction, "
                " pct_move, confidence, methods, first_seen_at) "
                "VALUES (:ticker,:pivot_date,:confirmed_on,:prior_direction,:new_direction,"
                "        :pct_move,:confidence,:methods,:first_seen_at)",
                {**row, "first_seen_at": datetime.datetime.now().isoformat(timespec="seconds")},
            )
            if cur.rowcount:  # 1 = actually inserted; 0 = ignored as a duplicate
                new_rows.append(row)
        conn.commit()
    finally:
        conn.close()
    return new_rows


def load_history(ticker: str | None = None) -> pd.DataFrame:
    conn = _connect()
    try:
        query = "SELECT * FROM detected_reversals"
        params: tuple = ()
        if ticker:
            query += " WHERE ticker = ?"
            params = (ticker,)
        query += " ORDER BY confirmed_on DESC, id DESC"
        return pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()
