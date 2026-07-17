"""
SQLite persistence layer.

This is the ONLY module that touches the database. populate.py (batch writer)
and app.py (read-only API) both import this and nothing else talks to SQL
directly. That separation is what lets app.py stay fast and simple: it never
computes anything, it only SELECTs precomputed rows.
"""
import os
import sqlite3
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "market.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    ticker TEXT NOT NULL,
    date   TEXT NOT NULL,      -- YYYY-MM-DD
    close  REAL NOT NULL,
    source TEXT NOT NULL,      -- 'stooq' | 'yfinance'
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS fred_series (
    series_id TEXT NOT NULL,
    date      TEXT NOT NULL,
    value     REAL NOT NULL,
    PRIMARY KEY (series_id, date)
);

CREATE TABLE IF NOT EXISTS fundamentals (
    ticker       TEXT PRIMARY KEY,
    name         TEXT,
    pe           REAL,
    pb           REAL,
    rating       TEXT,
    last_updated TEXT
);

-- One row per tracked instrument, refreshed by populate.py. app.py reads
-- ONLY this table for the dashboard -- never computes ratios/means live.
CREATE TABLE IF NOT EXISTS summary (
    key             TEXT PRIMARY KEY,   -- "sector:XLE" / "currency:EUR" / "commodity:Gold" / "fred:CSUSHPISA" / "country_etf:ENOR"
    category        TEXT NOT NULL,
    label           TEXT NOT NULL,
    ticker          TEXT,
    commodity_link  TEXT,               -- for convergence screen
    currency_link   TEXT,
    last_price      REAL,
    current_ratio   REAL,               -- current value / gold price
    historical_mean REAL,
    deviation_pct   REAL,               -- (current - mean) / mean * 100
    sentiment_label TEXT,
    sentiment_score REAL,
    n_observations  INTEGER,
    history_start   TEXT,
    last_updated    TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- prices ---
def delete_prices_before(cutoff_date: str):
    """Delete all price rows older than cutoff_date ('YYYY-MM-DD'). Returns
    rows deleted. Used by `python populate.py --trim` to reclaim disk space
    if history_start_date in tickers.yaml was tightened after data already
    existed."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM prices WHERE date < ?", (cutoff_date,))
        return cur.rowcount


def last_price_date(ticker: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(date) AS d FROM prices WHERE ticker = ?", (ticker,)
        ).fetchone()
    return row["d"] if row and row["d"] else None


def upsert_prices(ticker: str, rows, source: str):
    """rows: iterable of (date_str, close_float). Returns number of rows written."""
    if not rows:
        return 0
    with get_conn() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO prices (ticker, date, close, source) VALUES (?,?,?,?)",
            [(ticker, d, float(c), source) for d, c in rows],
        )
    return len(rows)


def get_price_series(ticker: str):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT date, close FROM prices WHERE ticker = ? ORDER BY date", (ticker,)
        ).fetchall()
    return [(r["date"], r["close"]) for r in rows]


# ------------------------------------------------------------ fred series ---
def upsert_fred(series_id: str, rows):
    if not rows:
        return 0
    with get_conn() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO fred_series (series_id, date, value) VALUES (?,?,?)",
            [(series_id, d, float(v)) for d, v in rows],
        )
    return len(rows)


def get_fred_series(series_id: str):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT date, value FROM fred_series WHERE series_id = ? ORDER BY date",
            (series_id,),
        ).fetchall()
    return [(r["date"], r["value"]) for r in rows]


# ------------------------------------------------------------ fundamentals --
def upsert_fundamentals(ticker: str, name, pe, pb, rating, last_updated):
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO fundamentals (ticker, name, pe, pb, rating, last_updated)
               VALUES (?,?,?,?,?,?)""",
            (ticker, name, pe, pb, rating, last_updated),
        )


def get_fundamentals(ticker: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM fundamentals WHERE ticker = ?", (ticker,)
        ).fetchone()
    return dict(row) if row else None


# ------------------------------------------------------------------ summary -
def upsert_summary(row: dict):
    cols = ",".join(row.keys())
    placeholders = ",".join("?" for _ in row)
    with get_conn() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO summary ({cols}) VALUES ({placeholders})",
            list(row.values()),
        )


def get_summary(category: str = None):
    with get_conn() as conn:
        if category:
            rows = conn.execute(
                "SELECT * FROM summary WHERE category = ? ORDER BY deviation_pct",
                (category,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM summary ORDER BY deviation_pct").fetchall()
    return [dict(r) for r in rows]


def get_summary_by_key(key: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM summary WHERE key = ?", (key,)).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------- meta -
def set_meta(key, value):
    with get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, str(value)))


def get_meta(key):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None
