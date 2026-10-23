"""개인화 리콜 메시지를 AI로 쓴다. 알림톡(템플릿) 규칙에는 쓰지 않는다."""
from __future__ import annotations

from ..drafting import DrafterError
from ..drafting.local import OllamaClient
from .prompts import SCHEMA, SYSTEM_PROMPT


def _checked(data: dict) -> dict:
    if not isinstance(data.get("message"), str) or not data["message"].strip():
        raise DrafterError("AI가 빈 메시지를 돌려줬어요.")
    return data


class LocalRecallWriter:
    """기본값. 이 컴퓨터의 Ollama로 쓰므로 비용이 들지 않는다."""

    mode = "local"

    def __init__(self, client: OllamaClient | None = None):
        self.client = client or OllamaClient()

    def write(self, prompt: str) -> dict:
        return _checked(self.client.chat_json(SYSTEM_PROMPT, prompt, SCHEMA, think=True))


class ClaudeRecallWriter:
    """유료 API. INQUIRY_HUB_LLM=claude일 때만 쓴다."""

    mode = "claude"

    def __init__(self, client=None, model: str | None = None):
        import anthropic

        from ..drafting.claude import MODEL

        self.client = client or anthropic.Anthropic()
        self.model = model or MODEL

    def write(self, prompt: str) -> dict:
        from ..drafting.claude import call_structured

        return _checked(call_structured(self.client, self.model, SYSTEM_PROMPT, prompt, SCHEMA))
