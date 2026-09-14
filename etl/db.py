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
#
# surprise/surprise_z/z_method/z_sample_n은 compute.py 전용 컬럼이다.
# insert_observation()(fetch_calendar.py가 씀)은 이 컬럼들을 절대 건드리지 않는다 —
# 안 그러면 fetch_calendar.py 재실행 시 매번 NULL로 덮어써서 compute.py가 계산한
# 값이 지워진다. z_method: 'zscore'(표본 충분) 또는 'raw'(표본 부족 - surprise를
# surprise_z 자리에 그대로 대입해 쓴다는 표시).
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
    surprise      REAL,
    surprise_z    REAL,
    z_method      TEXT,
    z_sample_n    INTEGER,
    PRIMARY KEY (indicator_id, ref_period, vintage_date)
);
"""

# key_indicators/position_plan은 SQLite에 배열/JSONB 타입이 없어 TEXT에 JSON
# 문자열로 저장한다 (forecast.py가 json.dumps/json.loads로 처리).
# actual_state/brier_score는 입력 시점엔 NULL - score_forecasts.py가 회의 이후
# FRED 데이터가 들어오면 채운다.
FORECAST_SCHEMA = """
CREATE TABLE IF NOT EXISTS forecasts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_date   TEXT NOT NULL,
    forecast_date  TEXT NOT NULL,
    q_hike_hawk    REAL NOT NULL,
    q_hike_dove    REAL NOT NULL,
    q_hold         REAL NOT NULL,
    q_cut_hawk     REAL NOT NULL,
    q_cut_dove     REAL NOT NULL,
    hold_tone      INTEGER,
    rationale      TEXT,
    key_indicators TEXT,
    position_plan  TEXT,
    actual_state   TEXT,
    brier_score    REAL
);
"""

# market_baseline: OIS 등 시장 내재확률. 현재는 무료 수집 소스가 없어 빈 테이블로
# 시작 - score_forecasts.py는 데이터가 없으면 "시장 비교 없음"으로 건너뛴다.
MARKET_BASELINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_baseline (
    meeting_date      TEXT NOT NULL,
    snapshot_date     TEXT NOT NULL,
    hike_prob         REAL,
    hold_prob         REAL,
    cut_prob          REAL,
    p_hike_hawk       REAL,
    p_hike_dove       REAL,
    p_cut_hawk        REAL,
    p_cut_dove        REAL,
    ust2y             REAL,
    implied_hawkish_z REAL,
    brier_score       REAL,
    PRIMARY KEY (meeting_date, snapshot_date)
);
"""


def _ensure_columns(conn, table, columns):
    """(name, decl) 목록 중 테이블에 없는 컬럼만 ALTER TABLE로 추가. 기존 커밋된
    macro.db도 안전하게 마이그레이션되도록 idempotent하게 동작한다."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(SCHEMA)
    conn.execute(OBS_SCHEMA)
    conn.execute(FORECAST_SCHEMA)
    conn.execute(MARKET_BASELINE_SCHEMA)
    _ensure_columns(conn, "observations", [
        ("surprise", "REAL"),
        ("z_method", "TEXT"),
        ("z_sample_n", "INTEGER"),
    ])
    conn.commit()
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


OBSERVATION_COLUMNS = [
    "indicator_id", "ref_period", "release_ts", "vintage_date",
    "actual", "actual_source", "consensus", "previous",
    "surprise", "surprise_z", "z_method", "z_sample_n",
]


def get_latest_observation(conn, indicator_id, ref_period):
    """(indicator_id, ref_period)의 가장 최근 vintage 행. 없으면 None."""
    cur = conn.execute(
        f"""
        SELECT {', '.join(OBSERVATION_COLUMNS)}
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
    return dict(zip(OBSERVATION_COLUMNS, row))


def insert_observation(conn, row):
    """row: dict (surprise/surprise_z/z_method/z_sample_n 키 없이). 오늘(vintage_date)
    행을 기록 — 날짜가 다르면 새 vintage, 같은 날 재실행이면 그대로 덮어써 멱등성을
    유지한다. compute.py 전용 컬럼(surprise*)은 절대 건드리지 않는다."""
    conn.execute(
        """
        INSERT INTO observations
            (indicator_id, ref_period, release_ts, vintage_date,
             actual, actual_source, consensus, previous)
        VALUES (:indicator_id, :ref_period, :release_ts, :vintage_date,
                :actual, :actual_source, :consensus, :previous)
        ON CONFLICT(indicator_id, ref_period, vintage_date) DO UPDATE SET
            release_ts    = excluded.release_ts,
            actual        = excluded.actual,
            actual_source = excluded.actual_source,
            consensus     = excluded.consensus,
            previous      = excluded.previous
        """,
        row,
    )
    conn.commit()


def update_surprise(conn, indicator_id, ref_period, vintage_date,
                     surprise, surprise_z, z_method, z_sample_n):
    """compute.py가 이미 존재하는 관측치 행에 surprise 관련 컬럼만 채워 넣는다."""
    conn.execute(
        """
        UPDATE observations
        SET surprise = ?, surprise_z = ?, z_method = ?, z_sample_n = ?
        WHERE indicator_id = ? AND ref_period = ? AND vintage_date = ?
        """,
        (surprise, surprise_z, z_method, z_sample_n, indicator_id, ref_period, vintage_date),
    )
    conn.commit()


FORECAST_COLUMNS = [
    "id", "meeting_date", "forecast_date",
    "q_hike_hawk", "q_hike_dove", "q_hold", "q_cut_hawk", "q_cut_dove",
    "hold_tone", "rationale", "key_indicators", "position_plan",
    "actual_state", "brier_score",
]


def insert_forecast(conn, row):
    """row: dict (id, actual_state, brier_score 없이). 삽입된 forecast의 id 반환."""
    cur = conn.execute(
        """
        INSERT INTO forecasts
            (meeting_date, forecast_date, q_hike_hawk, q_hike_dove, q_hold,
             q_cut_hawk, q_cut_dove, hold_tone, rationale, key_indicators, position_plan)
        VALUES (:meeting_date, :forecast_date, :q_hike_hawk, :q_hike_dove, :q_hold,
                :q_cut_hawk, :q_cut_dove, :hold_tone, :rationale, :key_indicators, :position_plan)
        """,
        row,
    )
    conn.commit()
    return cur.lastrowid


def get_forecasts(conn):
    cur = conn.execute(
        f"SELECT {', '.join(FORECAST_COLUMNS)} FROM forecasts ORDER BY meeting_date"
    )
    return [dict(zip(FORECAST_COLUMNS, row)) for row in cur.fetchall()]


def get_unscored_forecasts(conn):
    cur = conn.execute(
        f"SELECT {', '.join(FORECAST_COLUMNS)} FROM forecasts "
        f"WHERE actual_state IS NULL ORDER BY meeting_date"
    )
    return [dict(zip(FORECAST_COLUMNS, row)) for row in cur.fetchall()]


def update_forecast_score(conn, forecast_id, actual_state, brier_score):
    conn.execute(
        "UPDATE forecasts SET actual_state = ?, brier_score = ? WHERE id = ?",
        (actual_state, brier_score, forecast_id),
    )
    conn.commit()


MARKET_BASELINE_COLUMNS = [
    "meeting_date", "snapshot_date", "hike_prob", "hold_prob", "cut_prob",
    "p_hike_hawk", "p_hike_dove", "p_cut_hawk", "p_cut_dove",
    "ust2y", "implied_hawkish_z", "brier_score",
]


def get_market_baseline(conn, meeting_date):
    """meeting_date의 모든 스냅샷(발표 전 궤적)을 snapshot_date순으로."""
    cur = conn.execute(
        f"SELECT {', '.join(MARKET_BASELINE_COLUMNS)} FROM market_baseline "
        f"WHERE meeting_date = ? ORDER BY snapshot_date",
        (meeting_date,),
    )
    return [dict(zip(MARKET_BASELINE_COLUMNS, row)) for row in cur.fetchall()]


def update_market_baseline_score(conn, meeting_date, snapshot_date, brier_score):
    conn.execute(
        "UPDATE market_baseline SET brier_score = ? WHERE meeting_date = ? AND snapshot_date = ?",
        (brier_score, meeting_date, snapshot_date),
    )
    conn.commit()
