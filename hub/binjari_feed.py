"""빈자리 탭에 넘기는 바깥 입력(/binjari/feed.js). 빈자리는 이 값으로 기준 시각과 기존 예약을 받는다.

형식은 빈자리 README '다른 앱에 품기': window.__BINJARI_FEED__ = { v, now(+09:00), clinicName, sourceLabel, bookings }
를 넣고 'binjari-feed' 이벤트를 보낸다. 모양이 하나라도 틀리면 빈자리는 샘플의원 값으로 넘어가지 않고 오류 화면에서 멈춘다.
그래서 CRM 값은 여기서 먼저 검사해, 틀린 한 건 때문에 탭 전체가 멈추지 않게 한다.

- CRM의 '예정' 예약 중 시각이 있는 것만 넘긴다. 이름·전화·고객 ID는 넣지 않고, 예약 ID도 고객 ID에서 거꾸로 찾을 수 없는 차례 번호다.
- 데모 CRM은 고객이 11명뿐이라 그대로 넘기면 거의 모든 칸이 빈다. 그래서 날짜마다 정해진 규칙으로 '데모용 합성 예약'을 섞고,
  띠에 그렇게 적는다. 합성 예약은 CRM 예약과 사람·방 수(hub/binjari_rules.py)를 넘지 않게 채운다. 실제 연동에서는 CRM 예약만 넘기면 된다.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta

from .binjari_rules import CAPACITY, DAILY_MAX, FILLER_DAYS, SERVICES, clinic_hours, minutes_of, uses
from .crm import CRMAdapter
from .models import Appointment

FEED_EVENT = "binjari-feed"
_CONTOUR = re.compile(r"윤곽|광대|사각턱|턱끝|양악")
_REVISIT = re.compile(r"재상담|재진")
_TIME = re.compile(r"(\d{1,2}):(\d{2})(?::\d{2})?")


def service_for(appt: Appointment) -> str:
    """CRM 예약 종류 → 빈자리 안건. 소요 시간·사람·방은 빈자리 규칙(B01)이 정한다."""
    if appt.kind == "수술":
        return "surgery-full" if _CONTOUR.search(appt.procedure) else "surgery-half"  # 안면윤곽은 하루짜리
    if appt.kind == "시술":
        return "petit"
    if appt.kind == "상담":
        return "revisit-consult" if _REVISIT.search(appt.procedure) else "first-consult"
    return "postop-visit"  # 경과: 실밥·부목 제거, 경과 확인


def _clean_time(value: str) -> str | None:
    """'9:30', '14:00:00' 같은 내보내기 값도 빈자리가 받는 HH:MM으로. 읽을 수 없으면 None."""
    m = _TIME.fullmatch((value or "").strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def crm_bookings(crm: CRMAdapter, today: date) -> tuple[list[dict], int, int]:
    """(넘길 예약, 시각이 없어 뺀 수, 날짜·시각 형식이 틀려 뺀 수). 예약 ID는 build_feed가 붙인다."""
    bookings, untimed, broken = [], 0, 0
    for customer in crm.all_customers():
        for a in customer.appointments:
            if a.status != "예정":
                continue
            try:
                day = date.fromisoformat((a.date or "").strip())
            except ValueError:
                broken += 1
                continue
            if day < today:
                continue
            if not (a.time or "").strip():
                untimed += 1
                continue
            start = _clean_time(a.time)
            if start is None:
                broken += 1
                continue
            bookings.append({"service": service_for(a), "date": day.isoformat(), "start": start})
    return bookings, untimed, broken


class _DayLoad:
    """하루의 분 단위 사람·방 사용량. 합성 예약이 정원을 넘지 않게 본다."""

    def __init__(self):
        self.used: dict[str, list[int]] = defaultdict(lambda: [0] * 1440)
        self.count: dict[str, int] = defaultdict(int)

    def fits(self, service: str, start: str) -> bool:
        for limit in DAILY_MAX:
            if service in limit["counts"] and sum(self.count[s] for s in limit["counts"]) >= limit["max"]:
                return False
        return all(self.used[r][m] + n <= CAPACITY[r] for r, a, b, n in uses(service, start) for m in range(max(a, 0), min(b, 1440)))

    def add(self, service: str, start: str) -> None:
        self.count[service] += 1
        for r, a, b, n in uses(service, start):
            for m in range(max(a, 0), min(b, 1440)):
                self.used[r][m] += n


# 데모용 합성 예약 후보: 요일별 칸에서 날짜마다 정해진 대로 약 3/4을 고른다(실행할 때마다 같다).
_WEEKDAY = [("first-consult", "10:00"), ("petit", "10:30"), ("postop-visit", "11:00"), ("first-consult", "11:30"),
            ("revisit-consult", "14:00"), ("first-consult", "15:00"), ("petit", "15:30"), ("postop-visit", "16:00"),
            ("revisit-consult", "16:30"), ("first-consult", "17:00"), ("petit", "17:30"), ("revisit-consult", "18:00")]
_SURGERY = {1: ("surgery-half", "10:00"), 2: ("surgery-full", "10:00"), 3: ("surgery-half", "10:00"), 4: ("surgery-half", "14:00")}
_SATURDAY = [("first-consult", "10:00"), ("petit", "11:00"), ("first-consult", "12:30"), ("revisit-consult", "13:30"),
             ("postop-visit", "14:30")]


def filler_bookings(today: date, existing: list[dict] = (), days: int = FILLER_DAYS) -> list[dict]:
    """existing(CRM 예약)을 먼저 놓고, 그 위에 진료시간 안·정원 안에서만 합성 예약을 채운다."""
    hours = clinic_hours()
    loads: dict[str, _DayLoad] = defaultdict(_DayLoad)
    for b in existing:
        loads[b["date"]].add(b["service"], b["start"])
    bookings = []
    for offset in range(days):
        day = today + timedelta(days=offset)
        open_hours = hours["weekly"][["mon", "tue", "wed", "thu", "fri", "sat", "sun"][day.weekday()]]
        if open_hours is None:
            continue
        close = minutes_of(open_hours["close"])
        candidates = list(_SATURDAY if day.weekday() == 5 else _WEEKDAY)
        if day.weekday() in _SURGERY:
            candidates.insert(0, _SURGERY[day.weekday()])
        bits = int(hashlib.sha256(day.isoformat().encode()).hexdigest(), 16)
        load = loads[day.isoformat()]
        for i, (service, start) in enumerate(candidates):
            begin = minutes_of(start)
            within = begin + SERVICES[service]["minutes"] <= close and begin <= close - hours["lastEntryMinutesBeforeClose"]
            if (bits >> (2 * i)) & 3 and within and load.fits(service, start):
                load.add(service, start)
                bookings.append({"service": service, "date": day.isoformat(), "start": start, "filler": True})
    return bookings


def build_feed(crm: CRMAdapter, now: datetime, clinic_name: str, include_filler: bool = True) -> dict:
    today = now.date()
    crm_part, untimed, broken = crm_bookings(crm, today)
    filler = filler_bookings(today, crm_part) if include_filler else []
    bookings = sorted(crm_part + filler, key=lambda b: (b["date"], b["start"], b["service"]))
    # 차례 번호만 붙인다(CRM 예약은 A, 합성 예약은 F). 고객 ID에서 거꾸로 찾을 수 있는 해시를 쓰지 않는다.
    numbered = [{"id": f"{'F' if b.get('filler') else 'A'}{n:04d}", "service": b["service"], "date": b["date"], "start": b["start"]}
                for n, b in enumerate(bookings)]
    skipped = [f"시각 미정 {untimed}건"] * bool(untimed) + [f"형식 오류 {broken}건"] * bool(broken)
    label = f"CRM 예약 {len(crm_part)}건" + (f"({'·'.join(skipped)} 제외)" if skipped else "")
    if include_filler:
        label += " + 데모용 합성 예약"
    return {
        "v": 1,
        "now": now.replace(second=0, microsecond=0, tzinfo=None).isoformat() + "+09:00",  # 빈자리는 +09:00이 붙은 초 단위 시각만 받는다
        "clinicName": clinic_name,
        "sourceLabel": label,
        "bookings": {"bookings": numbered, "exceptions": []},
    }


def feed_js(feed: dict) -> str:
    # JSON을 그대로 스크립트에 넣으면 '</script>'나 U+2028 같은 글자가 스크립트를 깰 수 있어 이스케이프한다.
    payload = json.dumps(feed, ensure_ascii=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return f"window.__BINJARI_FEED__ = {payload};\nwindow.dispatchEvent(new Event({json.dumps(FEED_EVENT)}));\n"
