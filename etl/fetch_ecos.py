import csv
import logging
import os
import sys
from datetime import date
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import get_connection, upsert_series

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_ecos")

ROOT = Path(__file__).resolve().parent.parent
INDICATORS_CSV = ROOT / "config" / "indicators.csv"
ECOS_URL_TMPL = (
    "https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/100000/"
    "{stat_code}/{cycle}/{start}/{end}/{item_path}"
)
START_DATE = date(1990, 1, 1)


def load_api_key():
    load_dotenv(ROOT / ".env")
    key = os.environ.get("ECOS_API_KEY")
    if not key:
        raise RuntimeError("ECOS_API_KEY가 .env에 설정되어 있지 않습니다")
    return key


def load_ecos_indicators():
    with open(INDICATORS_CSV, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return [
            row for row in reader
            if row["source"] == "ecos" and row["id_verified"] == "Y"
        ]


def format_period(d, cycle):
    if cycle == "D":
        return d.strftime("%Y%m%d")
    if cycle == "M":
        return d.strftime("%Y%m")
    if cycle == "Q":
        return f"{d.year}Q{(d.month - 1) // 3 + 1}"
    if cycle == "A":
        return str(d.year)
    raise ValueError(f"지원하지 않는 cycle: {cycle}")


def parse_date(time_str, cycle):
    if cycle == "D":
        return f"{time_str[0:4]}-{time_str[4:6]}-{time_str[6:8]}"
    if cycle == "M":
        return f"{time_str[0:4]}-{time_str[4:6]}-01"
    if cycle == "Q":
        year, q = time_str.split("Q")
        month = (int(q) - 1) * 3 + 1
        return f"{year}-{month:02d}-01"
    if cycle == "A":
        return f"{time_str}-01-01"
    raise ValueError(f"지원하지 않는 cycle: {cycle}")


def fetch_series(stat_code, cycle, item_code, api_key):
    item_path = "/".join(quote(seg, safe="") for seg in item_code.split("/"))
    url = ECOS_URL_TMPL.format(
        key=api_key,
        stat_code=stat_code,
        cycle=cycle,
        start=format_period(START_DATE, cycle),
        end=format_period(date.today(), cycle),
        item_path=item_path,
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    payload = resp.json()

    if "RESULT" in payload:
        result = payload["RESULT"]
        raise RuntimeError(f"ECOS API 오류 {result.get('CODE')}: {result.get('MESSAGE')}")

    observations = payload["StatisticSearch"]["row"]
    rows = [
        (parse_date(obs["TIME"], cycle), float(obs["DATA_VALUE"]))
        for obs in observations
        if obs["DATA_VALUE"] not in (None, "")
    ]
    assert len(rows) > 0, f"{stat_code}/{item_code}: 유효한 관측치가 0건 (조용한 실패 의심)"
    return rows


def main():
    api_key = load_api_key()
    indicators = load_ecos_indicators()
    logger.info("대상 지표 %d개 (source=ecos, id_verified=Y)", len(indicators))

    conn = get_connection()
    success = 0
    failed = []

    for row in indicators:
        indicator_id = row["id"]
        source_id = row["source_id"]
        item_code = row["item_code"]
        cycle = row["freq"]
        try:
            rows = fetch_series(source_id, cycle, item_code, api_key)
            upsert_series(
                conn,
                [(indicator_id, d, v, None) for d, v in rows],
            )
            logger.info(
                "OK   %-20s %-10s/%-12s %d rows",
                indicator_id, source_id, item_code, len(rows),
            )
            success += 1
        except Exception as e:
            logger.error(
                "FAIL %-20s %-10s/%-12s %s",
                indicator_id, source_id, item_code, e,
            )
            failed.append(indicator_id)

    conn.close()

    logger.info("완료: 성공 %d / 실패 %d (총 %d)", success, len(failed), len(indicators))
    if failed:
        logger.warning("실패 목록: %s", ", ".join(failed))

    assert success > 0, "모든 시리즈 수집 실패 - API 키/네트워크를 확인하세요"


if __name__ == "__main__":
    main()
