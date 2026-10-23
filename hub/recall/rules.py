"""리콜 규칙: 어떤 고객에게, 언제, 어떤 종류의 메시지를 보낼지. 규칙 자체는 data/recall_rules.json에 있다."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from ..models import ConsentEvent, Customer


@dataclass
class RecallRule:
    id: str
    name: str
    kind: str  # 정보성 / 광고성
    delivery: str  # template(알림톡) / ai(개인화)
    trigger: str
    timing: str
    min_days: int
    max_days: int
    appointment_kinds: list[str] = field(default_factory=list)
    consult_statuses: list[str] = field(default_factory=list)
    skip_if_followup_booked: bool = False
    template: str = ""
    purpose: str = ""

    @property
    def is_ad(self) -> bool:
        return self.kind == "광고성"


@dataclass
class Hit:
    """규칙에 걸린 고객 한 명. anchor는 메시지를 부른 사건(그 수술, 그 상담)이라 같은 사건으로 두 번 보내지 않는다."""

    rule: RecallRule
    anchor: str
    reason: str
    values: dict
    blocked: str = ""  # 규칙 단계에서 이미 보낼 수 없다고 판단한 이유 (예: 동의 날짜 기록 없음)


def load_rules(path: Path) -> list[RecallRule]:
    return [RecallRule(**raw) for raw in json.loads(path.read_text(encoding="utf-8"))["rules"]]


def month_day(iso: str) -> str:
    d = date.fromisoformat(iso[:10])
    return f"{d.month}월 {d.day}일"


def _days_since(iso: str, target: date) -> int:
    return (target - date.fromisoformat(iso[:10])).days


def _add_years(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:  # 2월 29일
        return day.replace(year=day.year + years, day=28)


def find_hits(rule: RecallRule, customer: Customer, target: date, consents: list[ConsentEvent] = ()) -> list[Hit]:
    """consents: 동의 이력. 수신 동의 관련 규칙(처리 결과 안내, 2년 확인)에만 쓴다."""
    hits = []
    today = target.isoformat()
    if rule.trigger == "after_procedure":
        for a in customer.appointments:
            if a.kind not in rule.appointment_kinds or a.status != "완료":
                continue
            days = _days_since(a.date, target)
            if not rule.min_days <= days <= rule.max_days:
                continue
            if rule.skip_if_followup_booked and any(
                x.kind == "경과" and x.status == "예정" and x.date >= today for x in customer.appointments
            ):
                continue
            hits.append(Hit(rule, f"{a.kind}:{a.date}:{a.procedure}", f"{a.procedure} {a.kind} 후 {days}일째",
                            {"시술": a.procedure, "날짜": month_day(a.date), "경과일": days}))

    elif rule.trigger == "before_appointment":
        for a in customer.appointments:
            if a.status == "예정" and -_days_since(a.date, target) == rule.min_days:
                hits.append(Hit(rule, f"{a.kind}:{a.date}:{a.procedure}", f"{month_day(a.date)} {a.kind} 예약 ({a.procedure}) 하루 전",
                                {"날짜": month_day(a.date), "예약내용": a.procedure}))

    elif rule.trigger == "after_consult_no_booking" and customer.consultations:
        c = max(customer.consultations, key=lambda x: x.date)
        days = _days_since(c.date, target)
        booked = any(a.date >= c.date and a.status in ("예정", "완료") for a in customer.appointments)
        if c.status in rule.consult_statuses and rule.min_days <= days <= rule.max_days and not booked:
            hits.append(Hit(rule, f"상담:{c.date}:{c.procedure}", f"{month_day(c.date)} {c.procedure} 상담 후 {days}일째 · 예약 없음",
                            {"시술": c.procedure, "날짜": month_day(c.date), "경과일": days}))

    elif rule.trigger == "consent_changed":
        # 수신 동의·거부·철회는 의사를 밝힌 다음 날부터 14일 안에 보낸 곳 이름·사실과 날짜·처리 결과를 알려야 한다.
        for e in consents:
            days = _days_since(e.at, target)
            if e.field != "marketing_consent" or not rule.min_days <= days <= rule.max_days:
                continue
            deadline = date.fromisoformat(e.at[:10]) + timedelta(days=14)
            hits.append(Hit(rule, f"동의변경:{e.at}:{e.value}", f"{month_day(e.at)} {e.source} · {month_day(deadline.isoformat())}까지 알려야 해요",
                            {"처리": "동의" if e.value else "거부", "요청일": month_day(e.at), "처리일": month_day(e.at), "요청시각": e.at}))

    elif rule.trigger == "consent_anniversary" and customer.marketing_consent:
        # 광고 수신 동의는 동의한 날부터 2년마다, 그 해의 같은 날 전까지 동의 사실을 알려야 한다. 다시 동의했으면 그날이 기준이다.
        given = [e for e in consents if e.field == "marketing_consent" and e.value]
        if not given:  # 동의는 '예'인데 날짜가 없으면 기한을 모른다. 조용히 넘기지 않고 보류 목록에 띄운다
            hits.append(Hit(rule, "동의확인:날짜없음", "광고 수신 동의 날짜 기록이 없어요", {},
                            blocked="동의 날짜 기록이 없어 2년 확인 기한을 계산할 수 없어요. CRM에 동의 날짜와 경로를 넣어 주세요."))
        else:
            latest = max(given, key=lambda e: e.at)
            base = date.fromisoformat(latest.at[:10])
            years = 2
            while _add_years(base, years) <= target:
                years += 2
            due = _add_years(base, years)
            days_left = (due - target).days
            if rule.min_days <= days_left <= rule.max_days:
                last_day = (due - timedelta(days=1)).isoformat()
                hits.append(Hit(rule, f"동의확인:{due.isoformat()}", f"{base:%Y.%m.%d} {latest.source}에서 동의 · {month_day(last_day)}까지 알려야 해요",
                                {"동의일": f"{base.year}년 {month_day(base.isoformat())}", "동의경로": latest.source}))
    return hits


def next_visit_sentence(customer: Customer, target: date) -> str:
    upcoming = sorted((a for a in customer.appointments if a.status == "예정" and a.date >= target.isoformat()), key=lambda a: a.date)
    if not upcoming:
        return "경과 확인 일정은 따로 안내드리겠습니다."
    return f"다음 내원은 {month_day(upcoming[0].date)} {upcoming[0].procedure}입니다."


_VARIABLE = re.compile(r"\{(\w+)\}")


def fill_template(template: str, values: dict) -> str:
    """알림톡은 검수받은 문구에 변수만 바꿔 넣을 수 있다. 채우지 못한 변수는 그대로 남겨 화면에서 보이게 한다."""
    return _VARIABLE.sub(lambda m: str(values.get(m.group(1), m.group(0))), template)


def unfilled(text: str) -> list[str]:
    return _VARIABLE.findall(text)
