"""데모성형외과 진료시간. 안내 자료(data/knowledge/00_policy.md)의 문장에서 읽는다.

시드의 예정 방문을 휴진일·접수 마감 뒤에서 진료하는 날로 옮길 때 쓴다(hub/crm/seed.py).
문장이 바뀌어 읽지 못하면 옛 값을 조용히 쓰지 않고 멈춘다.
"""
from __future__ import annotations

import re
from pathlib import Path

KNOWLEDGE = Path(__file__).resolve().parent.parent / "data" / "knowledge"
DOWS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

_HOURS = re.compile(r"진료 시간은 평일 (오전|오후) (\d+)시부터 (오전|오후) (\d+)시까지, 토요일은 (오전|오후) (\d+)시부터 (오전|오후) (\d+)시까지입니다\.")
_LAST_ENTRY = re.compile(r"접수는 진료 종료 (\d+)분 전까지 받습니다\.")


def _hhmm(half: str, hour: str) -> str:
    h = int(hour)
    if half == "오후" and h != 12:
        h += 12
    return f"{h:02d}:00"


def clinic_hours() -> dict:
    """요일별 진료시간(일요일 휴진)과 접수 마감(진료 종료 몇 분 전)."""
    policy = (KNOWLEDGE / "00_policy.md").read_text(encoding="utf-8")
    hours, last_entry = _HOURS.search(policy), _LAST_ENTRY.search(policy)
    if not hours or not last_entry:
        raise ValueError("00_policy.md에서 진료 시간·접수 마감 문장을 읽지 못했어요. 문장이 바뀌었으면 hub/clinic_hours.py도 맞춰 주세요.")
    weekday = {"open": _hhmm(*hours.group(1, 2)), "close": _hhmm(*hours.group(3, 4))}
    saturday = {"open": _hhmm(*hours.group(5, 6)), "close": _hhmm(*hours.group(7, 8))}
    return {
        "weekly": {d: dict(weekday) for d in DOWS[:5]} | {"sat": saturday, "sun": None},
        "lastEntryMinutesBeforeClose": int(last_entry.group(1)),
    }


def minutes_of(hhmm: str) -> int:
    hour, minute = hhmm.split(":")
    return int(hour) * 60 + int(minute)
