"""FOMC 회의 일정 로더. config/fomc_calendar.csv는 federalreserve.gov 공식
발표 일정에서 가져온 값 - 2028년분은 연준이 아직 공개하지 않아 비어있다."""
import csv
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CALENDAR_CSV = ROOT / "config" / "fomc_calendar.csv"


def load_meetings():
    with open(CALENDAR_CSV, newline="", encoding="utf-8-sig") as f:
        return [
            {"meeting_date": row["meeting_date"], "start_date": row["start_date"],
             "has_sep": row["has_sep"] == "Y"}
            for row in csv.DictReader(f)
        ]


def upcoming_meetings(today=None):
    today = today or date.today()
    today_s = today.isoformat()
    return [m for m in load_meetings() if m["meeting_date"] >= today_s]


def next_meeting(today=None):
    meetings = upcoming_meetings(today)
    return meetings[0] if meetings else None


def days_until(meeting_date_str, today=None):
    today = today or date.today()
    d = date.fromisoformat(meeting_date_str)
    return (d - today).days
