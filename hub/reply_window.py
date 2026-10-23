"""채널별 답장 가능 시간.

인스타그램(Meta 메시징 API)은 고객의 마지막 메시지 후 24시간 안에만 병원이 답할 수 있다.
사람 상담원이 직접 답하는 경우에만 HUMAN_AGENT 태그를 달아 7일까지 늘릴 수 있다(자동 메시지는 안 된다).
한창구의 답장은 모두 실장이 승인해 보내는 사람의 답이라 태그 대상이다. 실제 연동에서는 앱 검수로 human_agent 권한을 받아야 한다.
다른 채널(카카오 상담톡·네이버 톡톡·홈페이지)은 이 데모에서 시간 제한을 두지 않는다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

STANDARD = {"instagram": timedelta(hours=24)}
HUMAN_AGENT = {"instagram": timedelta(days=7)}


@dataclass
class ReplyWindow:
    state: str  # none(제한 없음) · open(24시간 안) · human_agent(태그로 7일까지) · closed(지남)
    deadline: str  # 지금 상태에서 보낼 수 있는 마지막 시각
    tag: str | None
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


def _when(moment: datetime, now: datetime) -> str:
    return f"{moment:%H:%M}" if moment.date() == now.date() else f"{moment.month}/{moment.day} {moment:%H:%M}"


def reply_window(channel: str, last_inbound_at: str, now: datetime) -> ReplyWindow:
    if channel not in STANDARD:
        return ReplyWindow("none", "", None, "")
    last = datetime.fromisoformat(last_inbound_at)
    standard_end, human_end = last + STANDARD[channel], last + HUMAN_AGENT[channel]
    if now < standard_end:
        return ReplyWindow("open", standard_end.isoformat(timespec="minutes"), None,
                           f"인스타 답장 가능 · {_when(standard_end, now)}까지")
    if now < human_end:
        return ReplyWindow("human_agent", human_end.isoformat(timespec="minutes"), "HUMAN_AGENT",
                           f"24시간이 지났어요. 사람 상담원 답장(HUMAN_AGENT)으로 {_when(human_end, now)}까지 보낼 수 있어요.")
    return ReplyWindow("closed", human_end.isoformat(timespec="minutes"), None,
                       "인스타 답장 기한(마지막 메시지 후 7일)이 지났어요. 전화나 문자로 연락해 주세요.")
