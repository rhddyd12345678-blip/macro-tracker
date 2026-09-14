"""
FOMC 예측 사후 채점 (SPEC §2, §4).

판정 규칙 (§2):
    hike_hawk : Δ정책금리 > 0  且 회의 직후 2Y 변화 > +5bp
    hike_dove : Δ정책금리 > 0  且 2Y 변화 <= +5bp
    hold      : Δ정책금리 = 0
    cut_hawk  : Δ정책금리 < 0  且 2Y 변화 > +5bp
    cut_dove  : Δ정책금리 < 0  且 2Y 변화 <= +5bp

Δ정책금리는 us_fftr_upper(회의일 전후), 2Y 변화는 us_2y(회의일 전날 종가 대비
회의일 종가)로 계산한다. series_daily에 아직 해당 날짜 데이터가 없으면(회의가
아직 안 열렸거나 FRED가 아직 안 올렸으면) None을 반환하고 채점을 건너뛴다 -
에러가 아니라 "아직 채점 불가" 상태다.

Brier = Σ(p_i - o_i)² / 5  (SPEC §4) - 나와 시장을 같은 5구간 라벨 위에서 비교.
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import (get_connection, get_forecasts, get_market_baseline,
                 get_unscored_forecasts, update_forecast_score,
                 update_market_baseline_score)
from fomc_calendar import days_until, next_meeting

Q_FIELDS = ["q_hike_hawk", "q_hike_dove", "q_hold", "q_cut_hawk", "q_cut_dove"]
STATES = ["hike_hawk", "hike_dove", "hold", "cut_hawk", "cut_dove"]

POLICY_SERIES = "us_fftr_upper"
TWOY_SERIES = "us_2y"
HOLD_TOLERANCE_BP = 1.0     # 부동소수 오차 흡수용
TWOY_HAWK_THRESHOLD_BP = 5.0
POLICY_AFTER_LAG_DAYS = 3   # 발표 후 신규금리가 반영될 때까지 버퍼


def _nearest_on_or_before(conn, series_id, date_str):
    row = conn.execute(
        "SELECT date, value FROM series_daily WHERE series_id = ? AND date <= ? "
        "ORDER BY date DESC LIMIT 1",
        (series_id, date_str),
    ).fetchone()
    return row


def _nearest_on_or_after(conn, series_id, date_str):
    row = conn.execute(
        "SELECT date, value FROM series_daily WHERE series_id = ? AND date >= ? "
        "ORDER BY date ASC LIMIT 1",
        (series_id, date_str),
    ).fetchone()
    return row


def classify_actual_state(conn, meeting_date):
    """반환: {"state", "delta_policy_bp", "delta_2y_bp", ...} 또는 아직 데이터
    부족이면 None."""
    d = date.fromisoformat(meeting_date)
    before_cutoff = (d - timedelta(days=1)).isoformat()
    after_cutoff = (d + timedelta(days=POLICY_AFTER_LAG_DAYS)).isoformat()

    # 정책금리는 FOMC 결정일 다음 영업일부터 새 금리가 반영된다(발효일 lag).
    # 결정일 당일로 조회하면 아직 안 바뀐 값을 집어 Δ가 0으로 잘못 나온다.
    policy_effective_from = (d + timedelta(days=1)).isoformat()

    policy_before = _nearest_on_or_before(conn, POLICY_SERIES, before_cutoff)
    policy_after = _nearest_on_or_after(conn, POLICY_SERIES, policy_effective_from)
    # 2Y는 결정 발표 당일 종가가 이미 반응을 반영하므로 결정일 그대로 조회한다.
    twoy_before = _nearest_on_or_before(conn, TWOY_SERIES, before_cutoff)
    twoy_after = _nearest_on_or_after(conn, TWOY_SERIES, meeting_date)

    if not (policy_before and policy_after and twoy_before and twoy_after):
        return None
    # 아직 회의 이후 시간이 안 지났으면(정책금리 반영 버퍼 이전) 섣불리 채점하지 않는다
    if policy_after[0] > after_cutoff:
        return None

    delta_policy_bp = (policy_after[1] - policy_before[1]) * 100
    delta_2y_bp = (twoy_after[1] - twoy_before[1]) * 100

    if abs(delta_policy_bp) < HOLD_TOLERANCE_BP:
        state = "hold"
    elif delta_policy_bp > 0:
        state = "hike_hawk" if delta_2y_bp > TWOY_HAWK_THRESHOLD_BP else "hike_dove"
    else:
        state = "cut_hawk" if delta_2y_bp > TWOY_HAWK_THRESHOLD_BP else "cut_dove"

    return {
        "state": state,
        "delta_policy_bp": delta_policy_bp,
        "delta_2y_bp": delta_2y_bp,
        "policy_before": policy_before, "policy_after": policy_after,
        "twoy_before": twoy_before, "twoy_after": twoy_after,
    }


def brier_score(probs_pct, actual_state):
    """probs_pct: {"hike_hawk": 10, ...} (0~100). SPEC §4: Σ(p-o)²/5."""
    return sum(
        ((probs_pct.get(s, 0) / 100.0) - (1.0 if s == actual_state else 0.0)) ** 2
        for s in STATES
    ) / len(STATES)


def score_all_forecasts(conn):
    scored, pending = 0, 0
    for fc in get_unscored_forecasts(conn):
        result = classify_actual_state(conn, fc["meeting_date"])
        if result is None:
            pending += 1
            continue

        my_probs = {s: fc[f"q_{s}"] for s in STATES}
        my_brier = brier_score(my_probs, result["state"])
        update_forecast_score(conn, fc["id"], result["state"], my_brier)
        scored += 1

        for mb in get_market_baseline(conn, fc["meeting_date"]):
            if mb["brier_score"] is not None:
                continue
            market_probs = {
                "hike_hawk": mb["p_hike_hawk"], "hike_dove": mb["p_hike_dove"],
                "hold": mb["hold_prob"],
                "cut_hawk": mb["p_cut_hawk"], "cut_dove": mb["p_cut_dove"],
            }
            if any(v is None for v in market_probs.values()):
                continue
            mb_brier = brier_score(market_probs, result["state"])
            update_market_baseline_score(conn, mb["meeting_date"], mb["snapshot_date"], mb_brier)

    return {"scored": scored, "pending": pending}


CALIBRATION_BUCKETS = [(0, 20), (20, 40), (40, 60), (60, 80), (80, 100)]
MIN_CALIBRATION_N = 30  # 버킷당 최소 표본 없이는 캘리브레이션 곡선이 무의미


def _avg_market_brier(conn, meeting_dates):
    briers = []
    for md in meeting_dates:
        for mb in get_market_baseline(conn, md):
            if mb["brier_score"] is not None:
                briers.append(mb["brier_score"])
    return (sum(briers) / len(briers)) if briers else None


def build_calibration(scored_forecasts):
    """예측확률(q_*) vs 실제 적중 여부를 버킷화해 캘리브레이션 곡선 데이터를 만든다.
    표본이 부족하면 곡선 대신 표본 부족 상태만 반환한다."""
    points = []
    for fc in scored_forecasts:
        for s in STATES:
            points.append((fc[f"q_{s}"], 1.0 if s == fc["actual_state"] else 0.0))

    if len(points) < MIN_CALIBRATION_N:
        return {"ready": False, "n": len(points), "min_n": MIN_CALIBRATION_N, "buckets": []}

    buckets = []
    for lo, hi in CALIBRATION_BUCKETS:
        in_bucket = [o for p, o in points if lo <= p < hi or (hi == 100 and p == 100)]
        if not in_bucket:
            continue
        buckets.append({
            "range": f"{lo}-{hi}%",
            "predicted_mid": (lo + hi) / 2,
            "observed_freq": sum(in_bucket) / len(in_bucket) * 100,
            "n": len(in_bucket),
        })
    return {"ready": True, "n": len(points), "min_n": MIN_CALIBRATION_N, "buckets": buckets}


def build_scoreboard(conn):
    """report/build_chart.py가 그대로 data.json에 넣을 수 있는 요약 구조."""
    forecasts = get_forecasts(conn)
    scored = [f for f in forecasts if f["actual_state"] is not None]

    my_avg_brier = (sum(f["brier_score"] for f in scored) / len(scored)) if scored else None
    market_avg_brier = _avg_market_brier(conn, [f["meeting_date"] for f in scored])

    nm = next_meeting()
    next_meeting_info = (
        {"meeting_date": nm["meeting_date"], "has_sep": nm["has_sep"],
         "days_until": days_until(nm["meeting_date"])}
        if nm else None
    )

    return {
        "forecasts": [
            {
                "id": f["id"], "meeting_date": f["meeting_date"], "forecast_date": f["forecast_date"],
                "probs": {s: f[f"q_{s}"] for s in STATES},
                "hold_tone": f["hold_tone"], "rationale": f["rationale"],
                "actual_state": f["actual_state"], "brier_score": f["brier_score"],
            }
            for f in forecasts
        ],
        "cumulative": {
            "my_avg_brier": my_avg_brier,
            "market_avg_brier": market_avg_brier,
            "n_scored": len(scored),
            "n_total": len(forecasts),
        },
        "calibration": build_calibration(scored),
        "next_meeting": next_meeting_info,
        "note": "market_baseline은 아직 무료 수집 소스가 없어 비어있음 - 값이 채워지면 "
                "자동으로 나 vs 시장 비교에 반영됨. 캘리브레이션 곡선은 표본이 "
                f"{MIN_CALIBRATION_N}개(예측 {MIN_CALIBRATION_N // len(STATES)}건) 미만이면 표시 안 함.",
    }


if __name__ == "__main__":
    conn = get_connection()
    summary = score_all_forecasts(conn)
    print(f"채점 완료: {summary['scored']}건 / 대기(데이터 부족) {summary['pending']}건")
    conn.close()
