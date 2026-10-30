from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from ..clinic_hours import DOWS, clinic_hours, minutes_of
from ..models import Appointment, ConsentEvent, Consultation, Customer, resolve_relative


def _open_day(raw: dict, today: date) -> dict:
    """시드의 '+N일'은 요일을 모르고 정한 값이다. 시각이 있는 예정 예약이 휴진일(일요일)이나 접수 마감 뒤에 떨어지면
    진료하는 다음 날로 민다. (공휴일은 아직 보지 않고 일요일·영업시간만 본다.)"""
    appt = {**raw, "date": resolve_relative(raw["date"], today)}
    if appt.get("status", "예정") != "예정" or not appt.get("time"):
        return appt
    hours = clinic_hours()
    day = date.fromisoformat(appt["date"])
    for _ in range(7):
        weekly = hours["weekly"][DOWS[day.weekday()]]
        if weekly and minutes_of(appt["time"]) <= minutes_of(weekly["close"]) - hours["lastEntryMinutesBeforeClose"]:
            break
        day += timedelta(days=1)
    return {**appt, "date": day.isoformat()}


def load_seed_customers(path: Path, today: date) -> list[Customer]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        Customer(
            id=raw["id"],
            name=raw["name"],
            phone=raw.get("phone", ""),
            email=raw.get("email", ""),
            language=raw.get("language", "ko"),
            channel_ids=dict(raw.get("channel_ids", {})),
            tags=list(raw.get("tags", [])),
            consultations=[Consultation(**{**c, "date": resolve_relative(c["date"], today)}) for c in raw.get("consultations", [])],
            appointments=[Appointment(**_open_day(a, today)) for a in raw.get("appointments", [])],
            marketing_consent=bool(raw.get("marketing_consent", False)),
            kakao_friend=bool(raw.get("kakao_friend", False)),
        )
        for raw in data["customers"]
    ]


def load_seed_consents(path: Path, today: date) -> dict[str, list[ConsentEvent]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        raw["id"]: [ConsentEvent(**{**e, "at": resolve_relative(e["at"], today) + "T09:00", "note": e.get("note", "")})
                    for e in raw.get("consent_events", [])]
        for raw in data["customers"]
        if raw.get("consent_events")
    }
