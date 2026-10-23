"""외부 서비스가 보내는 수신거부 알림을 읽는다. 형식은 각 서비스 공식 문서의 예시를 따랐다.

- NHN Cloud SMS 웹훅, event "UNSUBSCRIBE": 080 수신거부. hooks[].recipientNo가 거부한 번호다.
- 카카오톡 채널 웹훅, event "added"/"blocked": 채널 추가·차단. id는 전화번호가 아니라 카카오 사용자 ID라서,
  카카오 로그인으로 그 ID를 CRM에 연결해 둔 고객만 찾을 수 있다. 헤더는 Authorization: KakaoAK {어드민 키}.
- 솔라피, 네이버 클라우드 SENS는 080 수신거부 웹훅이 없어서 거부 목록 조회 API를 주기적으로 불러 같은 처리를 한다.

웹훅은 같은 알림을 다시 보낼 수 있다. 수신거부 반영은 이미 같은 상태면 아무것도 하지 않으므로 여러 번 받아도 괜찮다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import KST


def to_kst(value: str) -> str:
    """'2026-10-22T01:02:03Z', '2026-10-22T10:02:03.000+09:00' → 한국 시간 '2026-10-22T10:02'."""
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is not None:
        moment = moment.astimezone(KST).replace(tzinfo=None)
    return moment.isoformat(timespec="minutes")


@dataclass
class Unsubscribe:
    phone: str
    at: str


def parse_nhn_unsubscribe(payload: dict) -> list[Unsubscribe]:
    if payload.get("event") != "UNSUBSCRIBE":
        return []  # 발송 결과 등 다른 이벤트는 여기서 다루지 않는다
    return [Unsubscribe(h["recipientNo"], to_kst(h["createdDateTime"])) for h in payload.get("hooks", []) if h.get("recipientNo")]


@dataclass
class ChannelEvent:
    user_id: str
    id_type: str  # app_user_id · open_id
    blocked: bool
    at: str


def parse_kakao_channel(payload: dict) -> ChannelEvent | None:
    if payload.get("event") not in ("added", "blocked") or not payload.get("id"):
        return None
    return ChannelEvent(str(payload["id"]), payload.get("id_type", ""), payload["event"] == "blocked", to_kst(payload["updated_at"]))
