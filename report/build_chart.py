import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "macro.db"
OUT_JSON = Path(__file__).resolve().parent / "data.json"

LOOKBACK_YEARS = 5

POLICY_SERIES = ["us_fftr_upper", "us_effr"]
YIELD_SERIES = ["us_2y", "us_10y"]


def load_series(conn, series_id):
    cur = conn.execute(
        """
        SELECT date, value FROM series_daily
        WHERE series_id = ? AND date >= date('now', ?)
        ORDER BY date
        """,
        (series_id, f"-{LOOKBACK_YEARS} years"),
    )
    return [{"date": d, "value": v} for d, v in cur.fetchall()]


def main():
    conn = sqlite3.connect(DB_PATH)
    data = {
        "policy_rate": {sid: load_series(conn, sid) for sid in POLICY_SERIES},
        "yields": {sid: load_series(conn, sid) for sid in YIELD_SERIES},
    }
    conn.close()

    OUT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"wrote {OUT_JSON}")


if __name__ == "__main__":
    main()
