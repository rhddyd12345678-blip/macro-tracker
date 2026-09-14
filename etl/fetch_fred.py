import csv
import logging
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import get_connection, upsert_series

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_fred")

ROOT = Path(__file__).resolve().parent.parent
INDICATORS_CSV = ROOT / "config" / "indicators.csv"
FRED_URL = "https://api.stlouisfed.org/fred/series/observations"


def load_api_key():
    load_dotenv(ROOT / ".env")
    key = os.environ.get("FRED_API_KEY")
    if not key:
        raise RuntimeError("FRED_API_KEY가 .env에 설정되어 있지 않습니다")
    return key


def load_fred_indicators():
    with open(INDICATORS_CSV, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return [
            row for row in reader
            if row["source"] == "fred" and row["id_verified"] == "Y"
        ]


def fetch_series(source_id, api_key):
    resp = requests.get(
        FRED_URL,
        params={"series_id": source_id, "api_key": api_key, "file_type": "json"},
        timeout=30,
    )
    resp.raise_for_status()
    observations = resp.json()["observations"]
    rows = [
        (obs["date"], float(obs["value"]))
        for obs in observations
        if obs["value"] != "."
    ]
    assert len(rows) > 0, f"{source_id}: 유효한 관측치가 0건 (조용한 실패 의심)"
    return rows


def main():
    api_key = load_api_key()
    indicators = load_fred_indicators()
    logger.info("대상 지표 %d개 (source=fred, id_verified=Y)", len(indicators))

    conn = get_connection()
    success = 0
    failed = []

    for row in indicators:
        indicator_id = row["id"]
        source_id = row["source_id"]
        try:
            rows = fetch_series(source_id, api_key)
            upsert_series(
                conn,
                [(indicator_id, date, value, None) for date, value in rows],
            )
            logger.info("OK   %-20s %-15s %d rows", indicator_id, source_id, len(rows))
            success += 1
        except Exception as e:
            logger.error("FAIL %-20s %-15s %s", indicator_id, source_id, e)
            failed.append(indicator_id)

    conn.close()

    logger.info("완료: 성공 %d / 실패 %d (총 %d)", success, len(failed), len(indicators))
    if failed:
        logger.warning("실패 목록: %s", ", ".join(failed))

    assert success > 0, "모든 시리즈 수집 실패 - API 키/네트워크를 확인하세요"


if __name__ == "__main__":
    main()
