from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..models import Analysis, KnowledgeChunk


class DrafterError(Exception):
    """초안 생성에 실패했을 때. 파이프라인은 이걸 받으면 규칙 기반 초안으로 대신한다."""


@dataclass
class DraftRequest:
    """AI에 넘기는 입력. 개인정보는 이미 토큰으로 가려진 상태여야 한다."""

    today: str
    clinic_name: str
    reply_channel: str
    customer_token: str
    registered: bool
    profile_lines: list[str]
    facts: dict
    messages: list[dict]
    knowledge: list[KnowledgeChunk] = field(default_factory=list)

    @property
    def query(self) -> str:
        return " ".join(m["text"] for m in self.messages)

    def render_prompt(self) -> str:
        lines = [
            f"[오늘 날짜] {self.today}",
            f"[답장 채널] {self.reply_channel}",
            "",
            "[CRM 고객 정보]",
        ]
        lines += [f"- {line}" for line in self.profile_lines] or ["- CRM 미등록 신규 문의"]
        lines += ["", "[문의 내용] (오래된 순)"]
        lines += [f"- {m['at']} · {m['channel']}: {m['text']}" for m in self.messages]
        lines += ["", "[병원 안내 자료]"]
        lines += [f'<source id="{c.id}" title="{c.title}">{c.text}</source>' for c in self.knowledge]
        lines += ["", "위 문의를 분석하고 답변 초안을 작성하세요."]
        return "\n".join(lines)


class Drafter(Protocol):
    mode: str

    def draft(self, request: DraftRequest) -> Analysis: ...
