"""수신거부를 CRM에 반영한다.

들어오는 길은 셋이다.
- 080 무료수신거부: 문자 대행사가 거부한 번호를 알려 준다(NHN Cloud는 웹훅, 솔라피·네이버 클라우드는 목록 조회). 광고 수신 동의를 끈다.
- 카카오톡 채널 차단·추가: 브랜드 메시지 I타입(구 친구톡)의 수신거부 방법이다. 채널 친구 여부를 바꾼다.
  범위를 밝히지 않은 수신거부는 그 병원 광고 전체를 거부한 것으로 본다는 KISA 해설에 따라, 차단하면 광고 수신 동의도 끈다.
  다시 추가했다고 광고에 다시 동의한 것은 아니라서 그때는 채널 친구 여부만 바꾼다.
- 문의함에 '수신거부', '광고 그만' 같은 답장이 오면: 법은 거부 의사를 밝히는 방법을 가리지 않으니 그대로 반영한다.

거부 의사는 병원에 닿은 순간부터 효력이 있다. 그래서 CRM 동의 이력(언제·무엇이·어떤 경로로)에 남기고,
그 고객에게 준비·예약 중이던 광고를 바로 멈춘다. 처리 결과는 14일 안에 고객에게 알려야 한다(리콜의 '처리 결과 안내').
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .crm import CRMAdapter
from .models import CHANNEL_LABELS, ConsentEvent, ContactLog, Customer, now_iso

_REQUEST = re.compile(
    r"수신\s*거부|(?:수신|광고|마케팅)\s*동의\s*(?:를\s*)?(?:철회|취소)"
    r"|(?:광고|홍보|이벤트|마케팅)[^.!?\n]{0,8}?(?:거부|그만|사절|차단|안\s*받|받지\s*(?:않|말)|안\s*보내|보내지\s*마|오지\s*않)"
    r"|문자\s*그만|(?:그만|이제)\s*보내지\s*마|연락\s*하지\s*마|unsubscribe",
    re.IGNORECASE,
)
# 거부를 풀어 달라거나, 한 적 없다거나, 방법을 묻는 말은 자동으로 반영하지 않고 상담실장이 확인한다.
_REVERSED = re.compile(
    r"수신\s*거부\s*(?:를|가)?\s*(?:해제|취소|철회|풀)|수신\s*거부\s*(?:를)?\s*(?:안\s*했|않았|한\s*적\s*(?:이\s*)?없|하지\s*않았|된\s*것\s*같)"
    r"|(?:다시|계속)\s*받"
)
_HOW = re.compile(r"어떻게|방법|되나요|되는지|가능한가요|가능한지|하는\s*법")
_SENTENCE = re.compile(r"(?<=[.!?。\n])\s*")


def opt_out_intent(text: str) -> str | None:
    """'request'(거부 요청이 분명함) · 'unclear'(수신거부 이야기지만 해제·부인·방법 문의일 수 있음) · None."""
    found = None
    for sentence in _SENTENCE.split(text or ""):
        if not _REQUEST.search(sentence):
            continue
        if _REVERSED.search(sentence) or _HOW.search(sentence):
            found = "unclear"
            continue
        return "request"
    return found


def is_opt_out_request(text: str) -> bool:
    return opt_out_intent(text) == "request"


def result_notice(clinic_name: str, at: str, language: str = "ko") -> str:
    """답장 앞에 붙이는 수신거부 처리 결과 안내. 법정 항목(보낸 곳 이름, 거부 사실과 날짜, 처리 결과)을 모두 담는다."""
    day = datetime.fromisoformat(at)
    if language == "ko":
        return (f"요청하신 대로 {day.month}월 {day.day}일 자로 {clinic_name}의 광고성 정보 수신을 거부 처리했어요. "
                "예약·진료 안내처럼 꼭 필요한 연락은 계속 드릴게요.")
    return (f"As requested, {clinic_name} has unsubscribed you from promotional messages as of "
            f"{day:%B} {day.day}. We will still contact you about appointments and care.")


def mentions_result_notice(text: str) -> bool:
    """보낸 답장에 처리 결과 안내가 들어 있었는지. 들어 있었으면 리콜에서 따로 보내지 않는다."""
    return "광고성 정보 수신을 거부 처리했어요" in text or "unsubscribed you from promotional messages" in text


@dataclass
class OptOutResult:
    customer_id: str | None
    changed: bool
    message: str
    stopped: int = 0  # 멈춘 리콜 메시지 수


_DESCRIBE = {
    ("marketing_consent", False): "광고 수신 동의를 철회했어요.",
    ("marketing_consent", True): "광고 수신에 다시 동의했어요.",
    ("kakao_friend", False): "카카오톡 채널을 차단했어요.",
    ("kakao_friend", True): "카카오톡 채널을 다시 추가했어요.",
}


@dataclass
class OptOutService:
    crm: CRMAdapter
    listeners: list[Callable[[str, ConsentEvent], int | None]] = field(default_factory=list)  # 멈춘 건수를 돌려준다

    def _apply(self, customer: Customer | None, changes: list[tuple[str, bool]], source: str, at: str | None,
               missing: str) -> OptOutResult:
        if customer is None:
            return OptOutResult(None, False, missing)
        changes = [(name, value) for name, value in changes if getattr(customer, name) != value]
        if not changes:  # 대행사가 같은 알림을 다시 보내도 이력이 늘지 않는다
            return OptOutResult(customer.id, False, f"{customer.name}님은 이미 반영돼 있어요.")
        at = at or now_iso()
        stopped, said = 0, []
        for name, value in changes:
            event = ConsentEvent(at=at, field=name, value=value, source=source)
            self.crm.update_consent(customer.id, event)
            self.crm.log_contact(customer.id, ContactLog(
                at=at, channel="동의 변경", summary=f"[{source}] {_DESCRIBE[name, value]}", reply="", handled_by="시스템",
            ))
            stopped += sum(listener(customer.id, event) or 0 for listener in self.listeners)
            said.append(_DESCRIBE[name, value])
        message = f"{customer.name}님: {' '.join(said)}"
        if stopped:
            message += f" 준비·예약 중이던 메시지 {stopped}건을 멈췄어요."
        return OptOutResult(customer.id, True, message, stopped)

    def from_080(self, phone: str, at: str | None = None) -> OptOutResult:
        """080 거부는 번호 단위다. 같은 번호의 고객이 여럿이면 모두 반영한다."""
        missing = "CRM에 없는 번호예요. 대행사 수신거부 목록에는 남아 있어 광고 문자가 가지 않아요."
        results = [self._apply(c, [("marketing_consent", False)], "080 수신거부", at, missing) for c in self.crm.find_all_by_phone(phone)]
        if not results:
            return OptOutResult(None, False, missing)
        if len(results) == 1:
            return results[0]
        return OptOutResult(results[0].customer_id, any(r.changed for r in results), " ".join(r.message for r in results),
                            sum(r.stopped for r in results))

    def from_kakao(self, kakao_user_id: str, blocked: bool, at: str | None = None) -> OptOutResult:
        changes = [("kakao_friend", False), ("marketing_consent", False)] if blocked else [("kakao_friend", True)]
        return self._apply(self.crm.find_by_channel("kakao", kakao_user_id), changes,
                           "카카오 채널 차단" if blocked else "카카오 채널 추가", at,
                           "CRM에 연결된 카카오 계정이 아니에요. 카카오 로그인으로 연결한 고객만 찾을 수 있어요.")

    def from_staff(self, customer_id: str, handled_by: str, at: str | None = None) -> OptOutResult:
        """상담실장이 고객 뜻을 확인하고 직접 반영한다 (전화로 들은 거부, 자동 반영하지 않은 애매한 답장 등)."""
        return self._apply(self.crm.get(customer_id), [("marketing_consent", False)], f"상담실장 확인 ({handled_by})", at,
                           "CRM 고객을 찾지 못했어요.")

    def from_message(self, customer_id: str | None, channel: str, text: str, at: str | None = None) -> OptOutResult | None:
        if not is_opt_out_request(text):
            return None
        customer = self.crm.get(customer_id) if customer_id else None
        return self._apply(customer, [("marketing_consent", False)], f"{CHANNEL_LABELS.get(channel, channel)} 답장 '수신거부'", at,
                           "CRM 고객이 아니라 바꿀 동의가 없어요. 신규 등록할 때 광고 수신 동의를 받지 않은 상태로 두세요.")
