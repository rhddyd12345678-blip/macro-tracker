"""
4사분면 예측 입력 CLI (SPEC §4, §9).

    python etl/forecast.py                     # 전부 대화형으로 입력
    python etl/forecast.py --meeting-date 2026-09-16 \\
        --q-hike-hawk 10 --q-hike-dove 15 --q-hold 60 --q-cut-hawk 5 --q-cut-dove 10 \\
        --rationale "..." --key-indicators us_cpi,us_nfp --hold-tone 1

인자로 준 값은 그대로 쓰고, 빠진 값만 대화형으로 물어본다 (전부 인자로 주면
프롬프트 없이 바로 저장 - 자동화/테스트용).

4사분면 확률 입력은 이 프로젝트의 본체라 자동화하지 않는다 (SPEC §9) - 이
CLI는 입력을 "기록"만 할 뿐, 확률을 대신 계산해주지 않는다.
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import get_connection, insert_forecast
from fomc_calendar import days_until, upcoming_meetings

Q_FIELDS = ["q_hike_hawk", "q_hike_dove", "q_hold", "q_cut_hawk", "q_cut_dove"]
Q_LABELS = {
    "q_hike_hawk": "매파적 인상 (hike_hawk)",
    "q_hike_dove": "비둘기적 인상 (hike_dove)",
    "q_hold": "동결 (hold)",
    "q_cut_hawk": "매파적 인하 (cut_hawk)",
    "q_cut_dove": "비둘기적 인하 (cut_dove)",
}
SUM_TOLERANCE = 0.5


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


def prompt_key_indicators():
    raw = input("핵심 근거 지표 (쉼표로 구분, 예: us_cpi,us_nfp): ").strip()
    return [s.strip() for s in raw.split(",") if s.strip()]


def prompt_rationale():
    print("판단 근거 (한 줄, 엔터로 종료):")
    return input("> ").strip()


def build_args_parser():
    p = argparse.ArgumentParser(description="FOMC 4사분면 예측 입력")
    p.add_argument("--meeting-date")
    for f in Q_FIELDS:
        p.add_argument(f"--{f.replace('_', '-')}", type=float, dest=f)
    p.add_argument("--hold-tone", type=int, choices=[-1, 0, 1])
    p.add_argument("--rationale")
    p.add_argument("--key-indicators", help="쉼표로 구분된 지표 id 목록")
    p.add_argument("--position-plan", help="JSON 문자열 (선택)")
    return p


def main():
    args = build_args_parser().parse_args()

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

    if all(getattr(args, f) is not None for f in Q_FIELDS):
        values = {f: getattr(args, f) for f in Q_FIELDS}
        validate_probabilities(values)
    else:
        values = prompt_probabilities()

    hold_tone = args.hold_tone
    if hold_tone is None and not fully_scripted:
        hold_tone = prompt_hold_tone()

    rationale = args.rationale
    if rationale is None:
        rationale = prompt_rationale()

    if args.key_indicators is not None:
        key_indicators = [s.strip() for s in args.key_indicators.split(",") if s.strip()]
    else:
        key_indicators = prompt_key_indicators()

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

    conn = get_connection()
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
