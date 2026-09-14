"""
4사분면 예측 입력 CLI (SPEC §4, §9).

    python etl/forecast.py                     # 전부 대화형으로 입력
    python etl/forecast.py --meeting-date 2026-09-16 \\
        --q-hike-hawk 10 --q-hike-dove 15 --q-hold 60 --q-cut-hawk 5 --q-cut-dove 10 \\
        --rationale "..." --key-indicators us_cpi,us_nfp --hold-tone 1

인자로 준 값은 그대로 쓰고, 빠진 값만 대화형으로 물어본다 (전부 인자로 주면
프롬프트 없이 바로 저장 - 자동화/테스트용).

검증:
  - 5구간 확률 합은 100(±0.5) - 안 맞으면 재입력/에러
  - rationale은 10자 미만이면 재입력/에러
  - key_indicators는 config/indicators.csv에 있는 id인지 확인
  - 같은 meeting_date에 예측이 이미 있으면 덮어쓸지 확인 (대화형은 y/N 프롬프트,
    완전 비대화형은 --force 없이 충돌하면 에러로 막음 - 조용히 지워지지 않는다)

4사분면 확률 입력은 이 프로젝트의 본체라 자동화하지 않는다 (SPEC §9) - 이
CLI는 입력을 "기록"만 할 뿐, 확률을 대신 계산해주지 않는다.
"""
import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import (delete_forecast, get_connection, get_forecasts_for_meeting,
                 insert_forecast)
from fomc_calendar import days_until, upcoming_meetings

ROOT = Path(__file__).resolve().parent.parent
INDICATORS_CSV = ROOT / "config" / "indicators.csv"

Q_FIELDS = ["q_hike_hawk", "q_hike_dove", "q_hold", "q_cut_hawk", "q_cut_dove"]
Q_LABELS = {
    "q_hike_hawk": "매파적 인상 (hike_hawk)",
    "q_hike_dove": "비둘기적 인상 (hike_dove)",
    "q_hold": "동결 (hold)",
    "q_cut_hawk": "매파적 인하 (cut_hawk)",
    "q_cut_dove": "비둘기적 인하 (cut_dove)",
}
SUM_TOLERANCE = 0.5
RATIONALE_MIN_LEN = 10


def load_valid_indicator_ids():
    with open(INDICATORS_CSV, newline="", encoding="utf-8-sig") as f:
        return {row["id"] for row in csv.DictReader(f)}


def prompt_meeting_date():
    meetings = upcoming_meetings()
    if meetings:
        print("다가오는 FOMC 회의:")
        for i, m in enumerate(meetings[:6], 1):
            d = days_until(m["meeting_date"])
            sep = " (SEP)" if m["has_sep"] else ""
            print(f"  {i}. {m['meeting_date']}{sep} - D-{d}")
        raw = input("번호를 고르거나 날짜(YYYY-MM-DD)를 직접 입력: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(meetings[:6]):
            return meetings[int(raw) - 1]["meeting_date"]
        return raw
    return input("회의일(YYYY-MM-DD): ").strip()


def prompt_probabilities():
    while True:
        values = {}
        for field in Q_FIELDS:
            while True:
                raw = input(f"{Q_LABELS[field]} 확률(%): ").strip()
                try:
                    values[field] = float(raw)
                    break
                except ValueError:
                    print("숫자로 입력하세요.")
        total = sum(values.values())
        if abs(total - 100) <= SUM_TOLERANCE:
            return values
        print(f"합이 {total}%입니다. 100%에 맞춰 다시 입력하세요.")


def validate_probabilities(values):
    total = sum(values[f] for f in Q_FIELDS)
    if abs(total - 100) > SUM_TOLERANCE:
        raise ValueError(f"5구간 확률 합이 100이어야 합니다 (현재 {total})")


def prompt_hold_tone():
    raw = input("hold_tone (매파 +1 / 중립 0 / 비둘기 -1, 비우면 생략): ").strip()
    if raw == "":
        return None
    if raw not in ("-1", "0", "1"):
        print("무시하고 생략합니다 (-1/0/1만 허용).")
        return None
    return int(raw)


def validate_key_indicators(key_indicators, valid_ids):
    unknown = [i for i in key_indicators if i not in valid_ids]
    if unknown:
        raise ValueError(
            f"indicators.csv에 없는 id: {', '.join(unknown)}"
        )


def prompt_key_indicators(valid_ids):
    while True:
        raw = input("핵심 근거 지표 (쉼표로 구분, 예: us_cpi,us_nfp): ").strip()
        key_indicators = [s.strip() for s in raw.split(",") if s.strip()]
        try:
            validate_key_indicators(key_indicators, valid_ids)
            return key_indicators
        except ValueError as e:
            print(f"{e} - 다시 입력하세요.")


def validate_rationale(rationale):
    if len(rationale.strip()) < RATIONALE_MIN_LEN:
        raise ValueError(f"rationale은 최소 {RATIONALE_MIN_LEN}자 이상이어야 합니다")


def prompt_rationale():
    print(f"판단 근거 (최소 {RATIONALE_MIN_LEN}자, 엔터로 종료):")
    while True:
        raw = input("> ").strip()
        try:
            validate_rationale(raw)
            return raw
        except ValueError as e:
            print(f"{e} - 다시 입력하세요.")


def build_args_parser():
    p = argparse.ArgumentParser(description="FOMC 4사분면 예측 입력")
    p.add_argument("--meeting-date")
    for f in Q_FIELDS:
        p.add_argument(f"--{f.replace('_', '-')}", type=float, dest=f)
    p.add_argument("--hold-tone", type=int, choices=[-1, 0, 1])
    p.add_argument("--rationale")
    p.add_argument("--key-indicators", help="쉼표로 구분된 지표 id 목록")
    p.add_argument("--position-plan", help="JSON 문자열 (선택)")
    p.add_argument("--force", action="store_true",
                    help="같은 meeting_date에 이미 예측이 있어도 묻지 않고 덮어씀")
    return p


def check_overwrite(conn, meeting_date, force, fully_scripted):
    """같은 meeting_date에 기존 예측이 있으면 덮어쓸지 확인. 지우기로 하면
    기존 행을 삭제하고, 취소하면 None을 반환해 호출부가 저장을 건너뛰게 한다."""
    existing = get_forecasts_for_meeting(conn, meeting_date)
    if not existing:
        return True

    print(f"\n{meeting_date}에 이미 예측 {len(existing)}건이 있습니다:")
    for f in existing:
        print(f"  id={f['id']} ({f['forecast_date']}): "
              f"hold={f['q_hold']}% rationale=\"{f['rationale']}\"")

    if force:
        proceed = True
    elif fully_scripted:
        raise ValueError(
            f"{meeting_date}에 이미 예측이 있습니다 (--force로 덮어쓰거나 다른 회의일을 쓰세요)"
        )
    else:
        raw = input("덮어쓸까요? 기존 예측을 지우고 새로 저장합니다 (y/N): ").strip().lower()
        proceed = raw == "y"

    if not proceed:
        return False

    for f in existing:
        delete_forecast(conn, f["id"])
    print(f"기존 {len(existing)}건 삭제함.")
    return True


def main():
    args = build_args_parser().parse_args()
    valid_ids = load_valid_indicator_ids()

    # 필수 항목(회의일/5구간확률/rationale/key_indicators)이 전부 인자로
    # 왔으면 완전 비대화형(자동화/테스트용) - hold_tone처럼 원래 선택인
    # 항목까지 프롬프트로 붙잡지 않는다.
    fully_scripted = (
        args.meeting_date is not None
        and all(getattr(args, f) is not None for f in Q_FIELDS)
        and args.rationale is not None
        and args.key_indicators is not None
    )

    meeting_date = args.meeting_date or prompt_meeting_date()

    conn = get_connection()
    if not check_overwrite(conn, meeting_date, args.force, fully_scripted):
        print("취소했습니다. 저장하지 않았습니다.")
        conn.close()
        return

    if all(getattr(args, f) is not None for f in Q_FIELDS):
        values = {f: getattr(args, f) for f in Q_FIELDS}
        validate_probabilities(values)
    else:
        values = prompt_probabilities()

    hold_tone = args.hold_tone
    if hold_tone is None and not fully_scripted:
        hold_tone = prompt_hold_tone()

    rationale = args.rationale
    if rationale is not None:
        validate_rationale(rationale)
    else:
        rationale = prompt_rationale()

    if args.key_indicators is not None:
        key_indicators = [s.strip() for s in args.key_indicators.split(",") if s.strip()]
        validate_key_indicators(key_indicators, valid_ids)
    else:
        key_indicators = prompt_key_indicators(valid_ids)

    position_plan = None
    if args.position_plan:
        position_plan = json.loads(args.position_plan)

    row = {
        "meeting_date": meeting_date,
        "forecast_date": date.today().isoformat(),
        **values,
        "hold_tone": hold_tone,
        "rationale": rationale,
        "key_indicators": json.dumps(key_indicators, ensure_ascii=False),
        "position_plan": json.dumps(position_plan, ensure_ascii=False) if position_plan else None,
    }

    forecast_id = insert_forecast(conn, row)
    conn.close()

    print(f"\n저장됨: forecast id={forecast_id}, meeting_date={meeting_date}")
    for f in Q_FIELDS:
        print(f"  {Q_LABELS[f]}: {values[f]}%")
    if hold_tone is not None:
        print(f"  hold_tone: {hold_tone}")
    print(f"  key_indicators: {key_indicators}")


if __name__ == "__main__":
    try:
        main()
    except ValueError as e:
        print(f"입력 오류: {e}", file=sys.stderr)
        sys.exit(1)
