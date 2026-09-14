"""
surprise_z 계산 + 매파 스코어 집계 (SPEC §6).

[1] surprise_z
    surprise   = actual - consensus
    surprise_z = surprise / rolling_std(surprise, 지난 24개월)

    표본(과거 24개월 내 surprise 개수)이 MIN_SAMPLE_FOR_Z 미만이면 표준편차
    추정이 불안정하므로, surprise_z 자리에 raw surprise를 그대로 넣고
    z_method='raw'로 표시한다 (표본이 쌓이면 다음 실행부터 자동으로
    z_method='zscore'로 전환됨 - 값을 손으로 바꿀 필요 없음).

[2] 매파 스코어
    target_cb=FED 且 use_as=policy_input 인 지표만, 주(월요일 시작) 단위로
    signs.py의 hawkish_score()를 그대로 재사용해 집계한다. 발표가 없는 주는
    시계열에서 아예 제외한다 (0점으로 채우면 "진짜 중립"과 "데이터 없음"이
    구분이 안 됨).

지금은 컨센서스/실측치가 거의 없어 대부분 빈 결과가 나오는 게 정상이다.
이 모듈은 데이터가 쌓일수록 자동으로 값이 채워지는 구조만 제공한다.
"""
import csv
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import DB_PATH, get_connection, update_surprise
from signs import hawkish_score

ROOT = Path(__file__).resolve().parent.parent
INDICATORS_CSV = ROOT / "config" / "indicators.csv"

ROLLING_STD_MONTHS = 24
MIN_SAMPLE_FOR_Z = 8


def load_indicator_meta():
    with open(INDICATORS_CSV, newline="", encoding="utf-8-sig") as f:
        return {row["id"]: row for row in csv.DictReader(f)}


def _months_before(ref_period, months):
    y, m, d = (int(p) for p in ref_period.split("-"))
    m -= months
    while m <= 0:
        m += 12
        y -= 1
    return f"{y:04d}-{m:02d}-{d:02d}"


def _latest_vintage_rows(conn, indicator_id):
    """지표 하나의 (ref_period별 최신 vintage) actual+consensus 행을 시간순으로."""
    cur = conn.execute(
        """
        SELECT ref_period, vintage_date, actual, consensus
        FROM observations o
        WHERE indicator_id = ?
          AND actual IS NOT NULL AND consensus IS NOT NULL
          AND vintage_date = (
              SELECT MAX(vintage_date) FROM observations o2
              WHERE o2.indicator_id = o.indicator_id AND o2.ref_period = o.ref_period
          )
        ORDER BY ref_period
        """,
        (indicator_id,),
    )
    return cur.fetchall()


def compute_surprises(conn):
    """observations에 surprise/surprise_z/z_method/z_sample_n을 채운다.
    반환: {"zscore": n, "raw": n, "indicators": n}"""
    indicator_ids = [
        row[0] for row in conn.execute(
            "SELECT DISTINCT indicator_id FROM observations "
            "WHERE actual IS NOT NULL AND consensus IS NOT NULL"
        )
    ]

    n_zscore = n_raw = 0
    for indicator_id in indicator_ids:
        rows = _latest_vintage_rows(conn, indicator_id)
        # (ref_period, vintage_date, actual, consensus) 시간순 리스트
        history = []  # (ref_period, surprise)
        for ref_period, vintage_date, actual, consensus in rows:
            surprise = actual - consensus
            window_start = _months_before(ref_period, ROLLING_STD_MONTHS)
            prior = [s for rp, s in history if window_start <= rp < ref_period]
            n = len(prior)

            if n >= MIN_SAMPLE_FOR_Z:
                std = float(np.std(prior, ddof=1))
                if std > 0:
                    surprise_z, z_method = surprise / std, "zscore"
                else:
                    surprise_z, z_method = surprise, "raw"
            else:
                surprise_z, z_method = surprise, "raw"

            update_surprise(conn, indicator_id, ref_period, vintage_date,
                             surprise, surprise_z, z_method, n)
            if z_method == "zscore":
                n_zscore += 1
            else:
                n_raw += 1

            history.append((ref_period, surprise))

    return {"zscore": n_zscore, "raw": n_raw, "indicators": len(indicator_ids)}


def _fed_policy_input_ids(indicators):
    return {
        iid for iid, meta in indicators.items()
        if meta["target_cb"] == "FED" and meta["use_as"] == "policy_input"
    }


def _week_start(dt):
    d = dt.date() if isinstance(dt, datetime) else dt
    return d - timedelta(days=d.weekday())


def _released_rows(conn, fed_policy_ids):
    """surprise가 계산된 (ref_period별 최신 vintage) 행 중 FED policy_input만."""
    if not fed_policy_ids:
        return []
    placeholders = ",".join("?" for _ in fed_policy_ids)
    cur = conn.execute(
        f"""
        SELECT indicator_id, ref_period, release_ts, actual, consensus, previous,
               surprise, surprise_z, z_method, z_sample_n
        FROM observations o
        WHERE surprise IS NOT NULL
          AND indicator_id IN ({placeholders})
          AND vintage_date = (
              SELECT MAX(vintage_date) FROM observations o2
              WHERE o2.indicator_id = o.indicator_id AND o2.ref_period = o.ref_period
          )
        ORDER BY release_ts
        """,
        list(fed_policy_ids),
    )
    return cur.fetchall()


def compute_hawkish_series(conn):
    """FED policy_input 지표의 주간 매파 스코어 시계열. 발표 없는 주는 제외."""
    indicators = load_indicator_meta()
    fed_policy_ids = _fed_policy_input_ids(indicators)
    rows = _released_rows(conn, fed_policy_ids)

    weeks = {}
    for (indicator_id, ref_period, release_ts, actual, consensus, previous,
         surprise, surprise_z, z_method, z_sample_n) in rows:
        release_date = datetime.fromisoformat(release_ts).date()
        wk = _week_start(release_date).isoformat()
        meta = indicators[indicator_id]
        component = {
            "indicator_id": indicator_id,
            "label": meta["name_ko"],
            "value": surprise_z,
            "method": z_method,
            "sample_n": z_sample_n,
            "hawkish_sign": int(meta["hawkish_sign"]),
            "weight": float(meta["weight"]),
            "contribution": surprise_z * int(meta["hawkish_sign"]) * float(meta["weight"]),
        }
        weeks.setdefault(wk, []).append(component)

    series = []
    for wk in sorted(weeks):
        components = weeks[wk]
        score = hawkish_score([
            {"surprise_z": c["value"], "hawkish_sign": c["hawkish_sign"], "weight": c["weight"]}
            for c in components
        ])
        series.append({
            "week_start": wk,
            "score": score,
            "n": len(components),
            "components": components,
        })

    return {
        "universe_size": len(fed_policy_ids),
        "series": series,
        "note": "target_cb=FED 且 use_as=policy_input만 집계. 발표(actual+consensus 확보)가 "
                "없는 주는 시계열에서 제외 (0점으로 채우면 '중립'과 '데이터 없음'이 "
                "구분 안 됨). z_method='raw'인 값은 표본 부족으로 surprise_z 대신 "
                "raw surprise를 그대로 쓴 것 - 참고용으로만 볼 것.",
    }


def compute_current_week_releases(conn):
    """이번 주(월~일) FED policy_input 지표의 발표/대기 현황."""
    indicators = load_indicator_meta()
    fed_policy_ids = _fed_policy_input_ids(indicators)
    if not fed_policy_ids:
        return {"week_start": None, "week_end": None, "rows": []}

    today = date.today()
    wk_start = _week_start(today)
    wk_end = wk_start + timedelta(days=6)

    placeholders = ",".join("?" for _ in fed_policy_ids)
    cur = conn.execute(
        f"""
        SELECT indicator_id, ref_period, release_ts, actual, consensus, previous,
               surprise, surprise_z, z_method, z_sample_n
        FROM observations o
        WHERE indicator_id IN ({placeholders})
          AND release_ts >= ? AND release_ts < ?
          AND vintage_date = (
              SELECT MAX(vintage_date) FROM observations o2
              WHERE o2.indicator_id = o.indicator_id AND o2.ref_period = o.ref_period
          )
        ORDER BY release_ts
        """,
        list(fed_policy_ids) + [wk_start.isoformat(), (wk_end + timedelta(days=1)).isoformat()],
    )

    out_rows = []
    for (indicator_id, ref_period, release_ts, actual, consensus, previous,
         surprise, surprise_z, z_method, z_sample_n) in cur.fetchall():
        meta = indicators[indicator_id]
        released = actual is not None
        out_rows.append({
            "indicator_id": indicator_id,
            "label": meta["name_ko"],
            "release_ts": release_ts,
            "status": "released" if released else "pending",
            "actual": actual,
            "consensus": consensus,
            "previous": previous,
            "surprise": surprise,
            "surprise_z": surprise_z,
            "z_method": z_method,
            "z_sample_n": z_sample_n,
            "hawkish_sign": int(meta["hawkish_sign"]),
            "weight": float(meta["weight"]),
            "contribution": (
                surprise_z * int(meta["hawkish_sign"]) * float(meta["weight"])
                if released and surprise_z is not None else None
            ),
        })

    return {"week_start": wk_start.isoformat(), "week_end": wk_end.isoformat(), "rows": out_rows}


if __name__ == "__main__":
    conn = get_connection()
    summary = compute_surprises(conn)
    print(f"surprise 계산: zscore {summary['zscore']}건 / raw {summary['raw']}건 "
          f"(대상 지표 {summary['indicators']}개)")

    hawkish = compute_hawkish_series(conn)
    print(f"매파 스코어 대상 지표: {hawkish['universe_size']}개, "
          f"시계열 {len(hawkish['series'])}주")
    if hawkish["series"]:
        last = hawkish["series"][-1]
        print(f"  최신 주({last['week_start']}): score={last['score']:.3f}, n={last['n']}")

    week = compute_current_week_releases(conn)
    print(f"이번 주({week['week_start']}~{week['week_end']}) 발표 현황: {len(week['rows'])}건")
    for r in week["rows"]:
        print(f"  {r['status']:8s} {r['indicator_id']:20s} "
              f"surprise_z={r['surprise_z']} ({r['z_method']}, n={r['z_sample_n']})")

    conn.close()
