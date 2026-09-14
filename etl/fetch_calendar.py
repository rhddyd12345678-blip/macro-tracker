"""
경제 캘린더 컨센서스 수집기.

소스: ForexFactory JSON 캘린더 미러(nfs.faireconomy.media). 비공식이지만
HTML 스크레이핑이 아닌 구조화된 JSON이고, "이번 주(±)" 창만 제공한다.
이 소스는 실측 결과 actual 필드를 아예 주지 않는다 (스키마에 키 자체가 없음).

actual 채우기 우선순위:
  1) 피드가 actual을 주면 그대로 사용 (발표 당시 속보치, actual_source='feed')
  2) 피드에 없으면 series_daily의 FRED 값으로 역산 (actual_source='fred_derived')
     - FRED는 개정 후 최신치라 발표 당시 시장이 본 숫자와 다를 수 있음. 최후 수단.
  3) 한 번 'feed'로 잠긴 (indicator_id, ref_period)는 이후 실행에서 'fred_derived'로
     절대 덮어쓰지 않는다 (NFP 등 대폭 개정 지표의 surprise_z 왜곡 방지).
"""
import csv
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import get_connection, get_latest_observation, insert_observation

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_calendar")

ROOT = Path(__file__).resolve().parent.parent
INDICATORS_CSV = ROOT / "config" / "indicators.csv"
MAPPING_CSV = ROOT / "config" / "ff_mapping.csv"
FF_FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# fred_derived 역산을 지원하는 transform만. 나머지(level_4wma, qoq_saar 등)는
# 계산이 애매해 이번 스코프에서 제외 — 피드 actual이 없으면 그냥 대기(None)한다.
SUPPORTED_TRANSFORMS = {"level", "mom_diff", "mom_pct", "yoy"}


def load_mapping():
    with open(MAPPING_CSV, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_indicator_meta():
    with open(INDICATORS_CSV, newline="", encoding="utf-8-sig") as f:
        return {row["id"]: row for row in csv.DictReader(f)}


def fetch_feed():
    resp = requests.get(FF_FEED_URL, timeout=30)
    resp.raise_for_status()
    return resp.json()


def find_feed_row(feed, ff_title):
    matches = [r for r in feed if r.get("country") == "USD" and r.get("title") == ff_title]
    if not matches:
        return None
    matches.sort(key=lambda r: r["date"])
    return matches[0]


def parse_ff_number(s):
    """'180K', '-0.3%', '3.90%' 같은 ForexFactory 표기를 float으로 변환."""
    if s is None:
        return None
    s = s.strip()
    if s == "":
        return None
    neg = s.startswith("-")
    s = s.lstrip("+-")
    mult = 1.0
    if s.endswith("%"):
        s = s[:-1]
    elif s and s[-1] in "KMBT":
        mult = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[s[-1]]
        s = s[:-1]
    s = s.replace(",", "")
    if s == "":
        return None
    value = float(s) * mult
    return -value if neg else value


def next_ref_period(conn, indicator_id, freq):
    """series_daily에 마지막으로 들어온 기간 다음 기간을 계산 (다음 발표분 기준시점)."""
    row = conn.execute(
        "SELECT MAX(date) FROM series_daily WHERE series_id = ?", (indicator_id,)
    ).fetchone()
    last_date = row[0] if row else None
    if last_date is None:
        return None

    y, m, d = (int(p) for p in last_date.split("-"))
    if freq == "M":
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return f"{y:04d}-{m:02d}-01"
    if freq == "Q":
        m += 3
        if m > 12:
            m -= 12
            y += 1
        return f"{y:04d}-{m:02d}-01"
    if freq == "W":
        return (date(y, m, d) + timedelta(days=7)).isoformat()
    raise ValueError(f"next_ref_period: 지원하지 않는 freq {freq}")


def compute_fred_derived(conn, indicator_id, transform, ref_period):
    """series_daily 값을 transform대로 변환해 컨센서스와 비교 가능한 actual을 만든다."""
    if transform not in SUPPORTED_TRANSFORMS:
        return None

    cur_row = conn.execute(
        "SELECT value FROM series_daily WHERE series_id = ? AND date = ?",
        (indicator_id, ref_period),
    ).fetchone()
    if cur_row is None:
        return None
    cur_val = cur_row[0]

    if transform == "level":
        return cur_val

    lag_months = {"mom_diff": 1, "mom_pct": 1, "yoy": 12}[transform]
    y, m, _ = (int(p) for p in ref_period.split("-"))
    m -= lag_months
    while m <= 0:
        m += 12
        y -= 1
    prev_period = f"{y:04d}-{m:02d}-01"

    prev_row = conn.execute(
        "SELECT value FROM series_daily WHERE series_id = ? AND date = ?",
        (indicator_id, prev_period),
    ).fetchone()
    if prev_row is None or prev_row[0] in (None, 0):
        return None
    prev_val = prev_row[0]

    if transform == "mom_diff":
        return cur_val - prev_val
    return (cur_val / prev_val - 1) * 100


def main():
    mapping = load_mapping()
    indicators = load_indicator_meta()
    feed = fetch_feed()
    logger.info("대상 지표 %d개, 이번 주 캘린더 이벤트 %d건", len(mapping), len(feed))

    conn = get_connection()
    success, skipped, failed = 0, 0, []

    for m in mapping:
        indicator_id = m["indicator_id"]
        ff_title = m["ff_title"]
        try:
            meta = indicators[indicator_id]
            freq = meta["freq"]
            transform = meta["transform"]

            feed_row = find_feed_row(feed, ff_title)
            if feed_row is None:
                logger.info("SKIP %-20s '%s' 이번 주 캘린더에 없음", indicator_id, ff_title)
                skipped += 1
                continue

            if freq == "D":
                ref_period = feed_row["date"][:10]
            else:
                ref_period = next_ref_period(conn, indicator_id, freq)
            if ref_period is None:
                logger.warning(
                    "SKIP %-20s series_daily에 기준 데이터가 없어 ref_period 계산 불가",
                    indicator_id,
                )
                skipped += 1
                continue

            consensus = parse_ff_number(feed_row.get("forecast"))
            previous = parse_ff_number(feed_row.get("previous"))
            feed_actual = parse_ff_number(feed_row.get("actual"))

            latest = get_latest_observation(conn, indicator_id, ref_period)

            if latest and latest["actual_source"] == "feed":
                # 이미 발표 당시 속보치를 잠가둔 기간 - 뒤늦은 fred_derived로 덮어쓰지 않는다
                actual, actual_source = latest["actual"], "feed"
            elif feed_actual is not None:
                actual, actual_source = feed_actual, "feed"
            else:
                derived = compute_fred_derived(conn, indicator_id, transform, ref_period)
                actual, actual_source = (derived, "fred_derived") if derived is not None else (None, None)

            insert_observation(conn, {
                "indicator_id": indicator_id,
                "ref_period": ref_period,
                "release_ts": feed_row["date"],
                "vintage_date": date.today().isoformat(),
                "actual": actual,
                "actual_source": actual_source,
                "consensus": consensus,
                "previous": previous,
            })

            logger.info(
                "OK   %-20s ref=%s actual=%s(%s) consensus=%s previous=%s",
                indicator_id, ref_period, actual, actual_source, consensus, previous,
            )
            success += 1
        except Exception as e:
            logger.error("FAIL %-20s '%s' %s", indicator_id, ff_title, e)
            failed.append(indicator_id)

    conn.close()

    logger.info(
        "완료: 성공 %d / 스킵(이번주 없음) %d / 실패 %d (총 %d)",
        success, skipped, len(failed), len(mapping),
    )
    if failed:
        logger.warning("실패 목록: %s", ", ".join(failed))

    assert len(failed) < len(mapping), "모든 지표 처리 실패 - 피드/매핑을 확인하세요"


if __name__ == "__main__":
    main()
