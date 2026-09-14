import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "macro.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS series_daily (
    series_id TEXT NOT NULL,
    date      TEXT NOT NULL,
    value     REAL,
    chg_bp    REAL,
    PRIMARY KEY (series_id, date)
);
"""


def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(SCHEMA)
    return conn


def upsert_series(conn, rows):
    """rows: iterable of (series_id, date, value, chg_bp) tuples."""
    conn.executemany(
        """
        INSERT INTO series_daily (series_id, date, value, chg_bp)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(series_id, date) DO UPDATE SET
            value = excluded.value,
            chg_bp = excluded.chg_bp
        """,
        rows,
    )
    conn.commit()
