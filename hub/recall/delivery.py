"""리콜 메시지를 어떤 경로로, 언제, 어떤 표시를 달아 보낼지.

경로
- 정보성(진료 안내): 검수된 템플릿 규칙은 알림톡. 수신 동의가 필요 없고, 실패하면 문자로 대체 발송하도록 대행사에 설정하는 게 보통이다.
  AI로 문구를 개인화하는 정보성 규칙은 알림톡에 실을 수 없어 문자(LMS)로 간다.
- 광고성(재방문 안내): 광고 수신 동의 고객에게만. 병원 카카오톡 채널 친구면 카카오 브랜드 메시지 I타입, 아니면 문자(LMS).
  친구톡은 2025-12-31에 종료되고 브랜드 메시지로 바뀌었다. I타입은 '광고 동의 + 채널 친구' 대상이고 수신거부는 채널 차단이다.
  O타입(같은 대상, 080 수신거부)도 있다. 채널 친구가 아닌 사람에게 보내는 M/N타입은 카카오 승인과 채널 친구 5만 명 이상 등
  조건이 있어 병원 하나가 쓰기 어렵다. 그래서 친구가 아니면 문자로 보낸다.
- 인스타그램 DM·카카오 상담톡·네이버 톡톡 상담은 고객이 먼저 보낸 문의에 답하는 창구라 리콜에 쓰지 않는다.
  (인스타는 고객의 마지막 메시지 후 24시간이 지나면 병원이 먼저 보낼 수 없다. 네이버는 먼저 보내는 '스마트알림'이 따로 있지만
  별도 템플릿 검수 상품이고 네이버 사용자에게만 가서, 이 데모는 카카오·문자만 다룬다.)
- 해외 번호에는 국내 문자·카카오 메시지가 닿지 않거나 080 무료 수신거부가 동작하지 않아, 코디네이터가 직접 안내하도록 보류한다.

답장
- 고객이 문자(대체 발송·광고 LMS)에 답장하면 발신번호로 가므로, 양방향 문자 수신을 연결하지 않으면 문의함에 들어오지 않는다.
  알림톡·브랜드 메시지 답장도 채널 1:1 채팅을 켜 둬야 문의함의 카카오 채널로 들어온다. 그래서 리콜 문구는 '답장' 대신
  대표번호와 카카오톡 채널 채팅을 안내한다.

광고 표시 (정보통신망법 제50조, 시행령 별표 6. 실제 문구는 KISA 안내서와 대행사 기준으로 다시 확인)
- 맨 앞에 '(광고)'와 보낸 곳·연락처, 맨 끝에 무료 수신거부 방법. 문자(LMS)는 제목도 '(광고)'로 시작한다.
- 실제 브랜드 메시지 연동에서는 요청에 광고 표시(isAd 등)를 켜면 카카오가 '(광고)'와 채널 차단 안내를 덧붙인다.
  대행사에 따라 본문의 표시를 지우거나 두 번 보이는 것을 감수한다. 이 데모는 플랫폼 표시를 흉내 낼 수 없어 본문에 넣는다.
- 광고 문자는 전송자격인증을 받은 문자 대행사에만 맡겨야 한다는 2026년 개정 정보통신망법 내용이 보도됐다.
  시행일과 세부는 law.go.kr에서 확인해야 한다. 브랜드 메시지·알림톡은 해당하지 않는 것으로 안다.

글자 수와 발송 시간은 대행사·카카오 정책으로 바뀔 수 있어, 실제 도입 전에 다시 확인해야 한다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from ..models import Customer
from .rules import RecallRule

SEND_TIME = time(10, 0)  # 기본 발송 시각


@dataclass(frozen=True)
class Channel:
    id: str
    label: str
    max_chars: int | None = None
    max_bytes: int | None = None
    opt_out: str | None = None  # 광고일 때 맨 끝에 넣어야 하는 무료 수신거부 방법 ('무료'라는 말이 들어가야 한다)
    ad_hours: tuple[time, time] | None = None  # 광고를 보낼 수 있는 시간 [시작, 끝)
    targeting: str | None = None  # 브랜드 메시지 타깃 유형


CHANNELS = {
    "alimtalk": Channel("alimtalk", "알림톡 (실패 시 문자 대체)", max_chars=1000),
    # 경로 id는 화면·DB·테스트가 바뀌지 않게 'friendtalk' 그대로 둔다.
    # 브랜드 메시지 텍스트형은 1,300자까지지만 구 친구톡 기준 1,000자로 보수적으로 막는다.
    "friendtalk": Channel(
        "friendtalk", "카카오 브랜드 메시지 (I타입·구 친구톡)", max_chars=1000,
        opt_out="무료수신거부: 카카오톡 채널 홈 > 채널 차단", ad_hours=(time(8, 0), time(20, 50)), targeting="I",
    ),
    # 야간(21~08시) 판단은 수신 시각 기준이고 문자는 이통사에서 늦게 도착할 수 있어, 카카오와 같이 20:50에 끊는다.
    "sms": Channel(
        "sms", "문자(LMS)", max_bytes=2000,
        opt_out="무료수신거부 080-000-0000", ad_hours=(time(8, 0), time(20, 50)),
    ),
}

_KR_MOBILE = re.compile(r"^01[016789]\d{7,8}$")


def domestic_mobile(phone: str) -> str | None:
    """국내 휴대폰 번호면 숫자만 남긴 번호를, 해외·유선 번호면 None을 돌려준다. +82 10…도 받는다."""
    digits = re.sub(r"\D", "", phone or "")
    if (phone or "").strip().startswith("+"):
        if not digits.startswith("82"):
            return None
        digits = "0" + digits[2:].lstrip("0")
    return digits if _KR_MOBILE.match(digits) else None


def route(rule: RecallRule, customer: Customer) -> tuple[str | None, str, str | None]:
    """(경로, 받는 번호, 못 보내는 이유)."""
    if not customer.phone:
        if customer.email or customer.channel_ids:
            return None, "", "휴대폰 번호가 없어요. 담당 코디네이터가 이메일 등으로 직접 안내해 주세요."
        return None, "", "휴대폰 번호가 없어 알림톡·문자를 보낼 수 없어요."
    if domestic_mobile(customer.phone) is None:
        return None, "", "해외 번호예요. 국내 문자·카카오 메시지가 닿지 않으니 담당 코디네이터가 WeChat·WhatsApp·이메일로 직접 안내해 주세요."
    if rule.is_ad:
        return ("friendtalk" if customer.kakao_friend else "sms"), customer.phone, None
    if rule.delivery == "template":
        return "alimtalk", customer.phone, None
    return "sms", customer.phone, None  # 정보성인데 문구를 개인화하는 규칙. 알림톡은 템플릿만 보낼 수 있다


def send_time(channel_id: str, is_ad: bool, wanted: datetime) -> datetime:
    """광고는 허용 시간 밖이면 다음 발송 가능 시각(SEND_TIME)으로 미룬다. 정보성은 그대로 보낸다."""
    hours = CHANNELS[channel_id].ad_hours
    if not is_ad or hours is None:
        return wanted
    start, end = hours
    if start <= wanted.time() < end:
        return wanted
    day = wanted.date() if wanted.time() < start else wanted.date() + timedelta(days=1)
    return datetime.combine(day, max(SEND_TIME, start))


def subject(channel_id: str, is_ad: bool, clinic_name: str) -> str | None:
    """문자(LMS)는 제목도 직접 넣는다. 광고면 제목도 맨 앞에 (광고). 대행사 기본 제목에 맡기지 않는다."""
    if channel_id != "sms":
        return None
    return f"(광고) {clinic_name} 안내" if is_ad else f"{clinic_name} 안내"


def euc_kr_bytes(text: str) -> int:
    """문자 대행사는 보통 EUC-KR 기준으로 센다(한글 2바이트, 영문·숫자 1바이트)."""
    return sum(1 if ord(ch) < 128 else 2 for ch in text)


def length_flags(channel_id: str, text: str) -> list[dict]:
    channel = CHANNELS[channel_id]
    if channel.max_chars and len(text) > channel.max_chars:
        return [{"phrase": f"{len(text)}자", "reason": f"{channel.label}은 {channel.max_chars}자까지 보낼 수 있어요.", "blocking": True}]
    if channel.max_bytes and (size := euc_kr_bytes(text)) > channel.max_bytes:
        return [{"phrase": f"{size}바이트", "reason": f"{channel.label}은 {channel.max_bytes}바이트까지 보낼 수 있어요.", "blocking": True}]
    return []
