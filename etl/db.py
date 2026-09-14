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

# actual_source: 'feed' (ForexFactory 등 캘린더가 발표 당시 속보치를 준 경우) 또는
# 'fred_derived' (캘린더에 actual이 없어 FRED 개정치로 역산한 경우). 한 번 'feed'로
# 잠기면 이후 실행에서 'fred_derived'로 절대 덮어쓰지 않는다 (fetch_calendar.py 참고).
OBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    indicator_id  TEXT NOT NULL,
    ref_period    TEXT NOT NULL,
    release_ts    TEXT,
    vintage_date  TEXT NOT NULL,
    actual        REAL,
    actual_source TEXT,
    consensus     REAL,
    previous      REAL,
    surprise_z    REAL,
    PRIMARY KEY (indicator_id, ref_period, vintage_date)
);
"""


def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(SCHEMA)
    conn.execute(OBS_SCHEMA)
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


def get_latest_observation(conn, indicator_id, ref_period):
    """(indicator_id, ref_period)의 가장 최근 vintage 행. 없으면 None."""
    cur = conn.execute(
        """
        SELECT indicator_id, ref_period, release_ts, vintage_date,
               actual, actual_source, consensus, previous, surprise_z
        FROM observations
        WHERE indicator_id = ? AND ref_period = ?
        ORDER BY vintage_date DESC
        LIMIT 1
        """,
        (indicator_id, ref_period),
    )
    row = cur.fetchone()
    if row is None:
        return None
    keys = ["indicator_id", "ref_period", "release_ts", "vintage_date",
            "actual", "actual_source", "consensus", "previous", "surprise_z"]
    return dict(zip(keys, row))


def insert_observation(conn, row):
    """row: dict. 오늘(vintage_date) 행을 기록 — 날짜가 다르면 새 vintage,
    같은 날 재실행이면 그대로 덮어써 멱등성을 유지한다."""
    conn.execute(
        """
        INSERT INTO observations
            (indicator_id, ref_period, release_ts, vintage_date,
             actual, actual_source, consensus, previous, surprise_z)
        VALUES (:indicator_id, :ref_period, :release_ts, :vintage_date,
                :actual, :actual_source, :consensus, :previous, :surprise_z)
        ON CONFLICT(indicator_id, ref_period, vintage_date) DO UPDATE SET
            release_ts    = excluded.release_ts,
            actual        = excluded.actual,
            actual_source = excluded.actual_source,
            consensus     = excluded.consensus,
            previous      = excluded.previous,
            surprise_z    = excluded.surprise_z
        """,
        row,
    )
    conn.commit()
