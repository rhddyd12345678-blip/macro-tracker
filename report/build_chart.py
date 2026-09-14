import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "etl"))
from analysis import run_kr_link_analysis
from compute import compute_current_week_releases, compute_hawkish_series
from db import get_connection
from score_forecasts import build_scoreboard

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
    conn = get_connection()
    data = {
        "policy_rate": {sid: load_series(conn, sid) for sid in POLICY_SERIES},
        "yields": {sid: load_series(conn, sid) for sid in YIELD_SERIES},
        "kr_link": run_kr_link_analysis(conn),
        "hawkish": compute_hawkish_series(conn),
        "current_week": compute_current_week_releases(conn),
        "fomc_scoreboard": build_scoreboard(conn),
    }
    conn.close()

    OUT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"wrote {OUT_JSON}")


if __name__ == "__main__":
    main()
