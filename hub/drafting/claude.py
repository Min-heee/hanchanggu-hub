"""Claude로 문의를 분류하고 답변 초안을 만든다. 출력은 JSON 스키마로 고정한다."""
from __future__ import annotations

import json

import anthropic

from ..models import Analysis
from .base import DraftRequest, DrafterError
from .prompts import SCHEMA, SYSTEM_PROMPT

MODEL = "claude-opus-5-5"


def call_structured(client: anthropic.Anthropic, model: str, system: str, prompt: str, schema: dict) -> dict:
    """JSON 스키마로 고정된 응답을 받는다. 실패는 모두 DrafterError로 바꿔 호출한 쪽이 대체 경로로 가게 한다."""
    try:
        response = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            # 안전 분류기가 거절하면 서버가 권장 모델로 한 번 더 시도한다.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.AuthenticationError as e:
        raise DrafterError("Claude API 인증에 실패했어요. API 키를 확인해 주세요.") from e
    except anthropic.RateLimitError as e:
        raise DrafterError("Claude 사용량 한도에 걸렸어요. 잠시 후 다시 생성해 주세요.") from e
    except anthropic.APIStatusError as e:
        if "credit balance" in str(e.message).lower():
            raise DrafterError("Claude API 크레딧이 부족해요. Console의 Plans & Billing에서 충전해 주세요.") from e
        raise DrafterError(f"Claude API 오류가 났어요 (HTTP {e.status_code}).") from e
    except anthropic.APIConnectionError as e:
        raise DrafterError("Claude API에 연결하지 못했어요.") from e

    if response.stop_reason == "refusal":
        raise DrafterError("Claude가 이 요청에 대한 작성을 거절했어요.")
    if response.stop_reason == "max_tokens":
        raise DrafterError("Claude 응답이 길이 제한에 걸려 잘렸어요.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise DrafterError("Claude 응답을 해석하지 못했어요.") from e


class ClaudeDrafter:
    mode = "claude"

    def __init__(self, client: anthropic.Anthropic | None = None, model: str = MODEL):
        self.client = client or anthropic.Anthropic()
        self.model = model

    def draft(self, request: DraftRequest) -> Analysis:
        data = call_structured(self.client, self.model, SYSTEM_PROMPT, request.render_prompt(), SCHEMA)
        try:
            return Analysis(**data)
        except TypeError as e:
            raise DrafterError("Claude 응답을 해석하지 못했어요.") from e
