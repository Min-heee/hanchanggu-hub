"""빈자리에 넘기는 데모성형외과 예약 칸 규칙. 규칙 문서(scripts/make_binjari_vault.py)와 데모용 합성 예약(hub/binjari_feed.py)이
같은 값을 쓰도록 한곳에 둔다.

진료시간·점심·접수 마감·수술 요일과 실밥 제거 시기는 안내 자료(data/knowledge)의 문장에서 읽는다. 문장이 바뀌어 읽지 못하면
옛 값을 조용히 쓰지 않고 멈춘다. 소요 시간·사람·방 수·수술 최소 준비 기간은 병원 확인 전의 [가정]이다.
"""
from __future__ import annotations

import re
from pathlib import Path

KNOWLEDGE = Path(__file__).resolve().parent.parent / "data" / "knowledge"
CLINIC = "데모성형외과"
PHONE = "02-000-0000"
DOWS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# ---- 안내 자료에서 읽는 값 -------------------------------------------------------

_HOURS = re.compile(r"진료 시간은 평일 (오전|오후) (\d+)시부터 (오전|오후) (\d+)시까지, 토요일은 (오전|오후) (\d+)시부터 (오전|오후) (\d+)시까지입니다\.")
_DETAILS = re.compile(r"점심시간 없이 진료하며, 접수는 진료 종료 (\d+)분 전까지 받습니다\. 수술 일정은 토요일에도 잡을 수 있습니다\.")
_STITCHES = re.compile(r"보통 수술 (\d+)~(\d+)일 뒤")


def _hhmm(half: str, hour: str) -> str:
    h = int(hour)
    if half == "오후" and h != 12:
        h += 12
    return f"{h:02d}:00"


def _read(name: str) -> str:
    return (KNOWLEDGE / name).read_text(encoding="utf-8")


def clinic_hours() -> dict:
    """00_policy.md의 진료 시간 문단 → 빈자리 V02 값과 근거 문장."""
    policy = _read("00_policy.md")
    hours, details = _HOURS.search(policy), _DETAILS.search(policy)
    if not hours or not details:
        raise ValueError("00_policy.md에서 진료 시간·점심·접수 마감 문장을 읽지 못했어요. 문장이 바뀌었으면 hub/binjari_rules.py도 맞춰 주세요.")
    weekday = {"open": _hhmm(*hours.group(1, 2)), "close": _hhmm(*hours.group(3, 4))}
    saturday = {"open": _hhmm(*hours.group(5, 6)), "close": _hhmm(*hours.group(7, 8))}
    return {
        "sentence": f"{hours.group(0)} {details.group(0)}",
        "weekly": {d: dict(weekday) for d in DOWS[:5]} | {"sat": saturday, "sun": None},
        "lunch": {"start": "13:00", "end": "14:00", "days": []},  # 점심시간 없음(days가 비면 빈자리는 점심을 두지 않는다)
        "lastEntryMinutesBeforeClose": int(details.group(1)),
        "noSurgeryDays": [],
    }


def stitch_window() -> tuple[int, int]:
    """코·눈 안내에서 '보통 수술 N~M일 뒤' 실밥(부목) 제거 시기. 두 안내가 다르면 멈춘다."""
    found = {_STITCHES.search(_read(name)).group(1, 2) for name in ("10_nose.md", "20_eye.md")}
    if len(found) != 1:
        raise ValueError(f"코·눈 안내의 실밥 제거 시기가 서로 달라요: {found}")
    first, last = next(iter(found))
    return int(first), int(last)


# ---- [가정] 값: 사람·방, 안건별 소요 시간 ----------------------------------------------

RESOURCES = {"doctor": "의료진", "coordinator": "코디", "nurse": "간호", "consultRoom": "상담실", "treatmentRoom": "처치실",
             "surgeryRoom": "수술실"}
CAPACITY = {"doctor": 2, "coordinator": 2, "nurse": 3, "consultRoom": 2, "treatmentRoom": 1, "surgeryRoom": 1}
BUFFERS = {"surgeryRoom": {"before": 0, "after": 30}}  # 수술실 정리 30분
SURGERY_MIN_DAYS_AHEAD = 7  # 수술 전 검사·주의사항 안내에 일주일[가정]
SEARCH_DAYS = {"default": 14, "surgery": 28, "more": 14}
DAILY_MAX = [{"service": "first-consult", "max": 6, "counts": ["first-consult"]}]


def _grid(label, minutes, phases, notes=(), bookable=True):
    return {"label": label, "bookable": bookable, "minutes": minutes, "phases": phases, "starts": "grid",
            "lunch": "avoid", "earliest": "lead", "deposit": None, "notes": list(notes)}


def _surgery(label, minutes, starts):
    return {"label": label, "bookable": True, "minutes": minutes,
            "phases": [{"from": 0, "to": minutes, "use": {"doctor": 1, "nurse": 2, "surgeryRoom": 1}}],
            "starts": starts, "lunch": "through", "earliest": "surgery", "deposit": None,
            "notes": ["수술 예약금·변경·환불 기준은 상담 때 서면 안내(00_policy)"]}


SERVICES = {
    "first-consult": _grid("첫 상담(원장·실장)", 60, [
        {"from": 0, "to": 20, "use": {"coordinator": 1}}, {"from": 20, "to": 40, "use": {"doctor": 1}},
        {"from": 40, "to": 60, "use": {"coordinator": 1}}, {"from": 0, "to": 60, "use": {"consultRoom": 1}}],
        notes=["안면윤곽은 상담 당일 CT 촬영이 있어 시간을 넉넉히(30_contour_petit)", "통역이 필요하면 언어 확인(00_policy)"]),
    "revisit-consult": _grid("원장 재진 상담", 30, [{"from": 0, "to": 30, "use": {"doctor": 1, "consultRoom": 1}}],
                             notes=["바로 수술 아님 — 상담 뒤 수술은 '시술 → 수술'로 잡음"]),
    "petit": _grid("쁘띠 시술(필러·보톡스)", 30, [
        {"from": 0, "to": 15, "use": {"doctor": 1}}, {"from": 0, "to": 30, "use": {"nurse": 1, "treatmentRoom": 1}}],
        notes=["같은 날 여러 시술 여부는 원장이 결정(30_contour_petit)"]),
    "surgery-half": _surgery("수술(코·눈, 반나절)", 240, ["10:00", "14:00"]),
    "surgery-full": _surgery("수술(안면윤곽, 하루)", 480, ["10:00"]),
    "postop-visit": _grid("경과 진료(실밥·부목 제거, 경과 확인)", 30, [
        {"from": 0, "to": 10, "use": {"doctor": 1}}, {"from": 0, "to": 30, "use": {"nurse": 1, "treatmentRoom": 1}}],
        bookable=False),
}

# 데모용 합성 예약이 덮어야 하는 기간: 수술 첫 가능일 + 수술 검색 기간 + '더 보기' 두 번
FILLER_DAYS = SURGERY_MIN_DAYS_AHEAD + SEARCH_DAYS["surgery"] + 2 * SEARCH_DAYS["more"]


def minutes_of(hhmm: str) -> int:
    hour, minute = hhmm.split(":")
    return int(hour) * 60 + int(minute)


def uses(service: str, start: str) -> list[tuple[str, int, int, int]]:
    """(사람·방, 시작 분, 끝 분, 수) — 정리 시간(BUFFERS) 포함."""
    base = minutes_of(start)
    out = []
    for phase in SERVICES[service]["phases"]:
        for resource, count in phase["use"].items():
            buffer = BUFFERS.get(resource, {"before": 0, "after": 0})
            out.append((resource, base + phase["from"] - buffer["before"], base + phase["to"] + buffer["after"], count))
    return out
