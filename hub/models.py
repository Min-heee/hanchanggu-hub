from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

# 광고 야간 제한(21~08시) 같은 규칙은 한국 시간 기준이라, 서버가 UTC로 돌아도 병원 시계를 쓴다.
# 한국은 서머타임이 없어 고정 +9시간으로 충분하다 (윈도에서 tzdata 없이도 동작).
KST = timezone(timedelta(hours=9), "KST")

CHANNEL_LABELS = {
    "kakao": "카카오톡",
    "naver": "네이버 톡톡",
    "instagram": "인스타그램 DM",
    "web": "홈페이지 상담",
}

CATEGORIES = ["코성형", "눈성형", "안면윤곽", "가슴성형", "지방흡입", "리프팅·쁘띠", "기타"]
INTENTS = ["가격 문의", "예약·일정", "회복·경과", "시술 상담", "이상 증상", "불만·클레임", "기타"]
URGENCIES = ["high", "normal", "low"]
LANGUAGES = ["ko", "en", "zh", "ja", "other"]


@dataclass
class InboundMessage:
    id: str
    channel: str
    channel_user_id: str
    sender_name: str
    text: str
    received_at: str
    phone: str = ""
    email: str = ""


@dataclass
class Consultation:
    date: str
    procedure: str
    counselor: str
    status: str
    note: str = ""


@dataclass
class Appointment:
    date: str
    kind: str
    procedure: str
    status: str = "예정"
    time: str = ""  # 시작 시각 HH:MM. 비어 있으면 시각 미정


@dataclass
class ContactLog:
    at: str
    channel: str
    summary: str
    reply: str
    handled_by: str


CONSENT_FIELDS = ("marketing_consent", "kakao_friend")


@dataclass
class ConsentEvent:
    """수신 동의·채널 친구 여부가 바뀐 기록. 언제, 무엇이, 어떤 경로로 바뀌었는지 남겨야 나중에 증명할 수 있다."""

    at: str
    field: str  # marketing_consent(광고 수신 동의) · kakao_friend(카카오 채널 친구)
    value: bool
    source: str  # 예: 상담 동의서, 080 수신거부, 카카오 채널 차단, 문의 답장 '수신거부'
    note: str = ""


@dataclass
class Customer:
    id: str
    name: str
    phone: str = ""
    email: str = ""
    language: str = "ko"
    channel_ids: dict[str, str] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    consultations: list[Consultation] = field(default_factory=list)
    appointments: list[Appointment] = field(default_factory=list)
    marketing_consent: bool = False  # 광고성 정보 수신 동의 (정보통신망법 제50조)
    kakao_friend: bool = False  # 병원 카카오톡 채널을 친구 추가했는지. 브랜드 메시지 I타입(구 친구톡)은 채널 친구에게만 간다


@dataclass
class KnowledgeChunk:
    id: str
    title: str
    category: str
    text: str


@dataclass
class Analysis:
    category: str
    intent: str
    urgency: str
    language: str
    summary: str
    needs_human: bool
    needs_human_reason: str
    draft_reply: str
    draft_reply_ko: str
    used_sources: list[str]


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("82") and len(digits) >= 11:
        digits = "0" + digits[2:]
    return digits


_RELATIVE = re.compile(r"^([+-]\d+)d(?: (\d{2}:\d{2}))?$")


def resolve_relative(value: str, today: date) -> str:
    """'-7d' -> 7일 전 날짜, '-0d 09:05' -> 오늘 09:05. 이미 절대값이면 그대로 둔다."""
    match = _RELATIVE.match(value or "")
    if not match:
        return value
    day = today + timedelta(days=int(match.group(1)))
    if match.group(2):
        hour, minute = map(int, match.group(2).split(":"))
        return datetime(day.year, day.month, day.day, hour, minute).isoformat(timespec="minutes")
    return day.isoformat()


def now_kst() -> datetime:
    """병원 벽시계 시간(한국 시간)을 시간대 정보 없는 datetime으로 돌려준다."""
    return datetime.now(KST).replace(tzinfo=None)


def now_iso() -> str:
    return now_kst().isoformat(timespec="minutes")
