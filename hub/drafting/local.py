"""로컬 AI(Ollama)로 초안을 만든다. 비용이 들지 않고, 문의 내용이 이 컴퓨터 밖으로 나가지 않는다.

8B 모델은 분류를 자주 틀리고, 생각 단계 없이 쓰면 날짜 계산(수술 후 며칠째 → 냉·온찜질)을 틀렸다.
그래서 분류·긴급도는 규칙 엔진이 정하고, 모델은 생각 단계를 켠 채 답변 문장만 쓴다.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import replace

from ..models import Analysis
from .base import DraftRequest, DrafterError
from .prompts import LOCAL_SCHEMA, LOCAL_SYSTEM_PROMPT
from .rules import RuleDrafter

DEFAULT_MODEL = "qwen3:8b"
DEFAULT_URL = "http://127.0.0.1:11434"


class OllamaClient:
    def __init__(self, model: str | None = None, base_url: str | None = None, timeout: float = 240):
        self.model = model or os.environ.get("OLLAMA_MODEL") or DEFAULT_MODEL
        self.base_url = (base_url or os.environ.get("OLLAMA_URL") or DEFAULT_URL).rstrip("/")
        self.timeout = timeout

    def chat_json(self, system: str, prompt: str, schema: dict, think: bool = False) -> dict:
        """JSON 스키마로 출력을 묶어서 받는다(Ollama structured outputs).

        think는 Qwen3 같은 추론 모델의 생각 단계. 켜면 10초쯤 느려지지만 날짜·기간 판단이 정확해진다.
        """
        body = {
            "model": self.model,
            "stream": False,
            "think": think,
            "format": schema,
            "options": {"temperature": 0.3, "num_ctx": 8192},  # 프롬프트가 기본 문맥 길이보다 길 수 있다
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        }
        request = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.load(response)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise DrafterError(f"로컬 모델 {self.model}이 없어요. 터미널에서 'ollama pull {self.model}'을 실행해 주세요.") from e
            raise DrafterError(f"로컬 AI 오류가 났어요 (HTTP {e.code}).") from e
        except TimeoutError as e:
            raise DrafterError("로컬 AI 응답이 너무 오래 걸려요. 잠시 후 다시 생성해 주세요.") from e
        except urllib.error.URLError as e:
            raise DrafterError("로컬 AI(Ollama)가 꺼져 있어요. 터미널에서 'ollama serve'를 실행해 주세요.") from e

        try:
            return json.loads(data["message"]["content"])
        except (KeyError, TypeError, json.JSONDecodeError) as e:
            raise DrafterError("로컬 AI 응답을 해석하지 못했어요.") from e


class LocalDrafter:
    mode = "local"

    def __init__(self, client: OllamaClient | None = None):
        self.client = client or OllamaClient()
        self.rules = RuleDrafter()

    @property
    def model(self) -> str:
        return self.client.model

    def draft(self, request: DraftRequest) -> Analysis:
        base = self.rules.draft(request)
        if base.urgency == "high":  # 응급 안내는 모델이 바꾸지 못하게 검증된 고정 문구로 보낸다
            return base
        label = f"{base.category} · {base.intent} · 긴급도 {base.urgency} · 언어 {base.language}"
        if base.needs_human:
            label += f" · 담당자 확인 필요: {base.needs_human_reason}"
        prompt = f"[분류 결과] {label}\n\n{request.render_prompt()}"
        data = self.client.chat_json(LOCAL_SYSTEM_PROMPT, prompt, LOCAL_SCHEMA, think=True)
        try:
            return replace(
                base,
                summary=data["summary"],
                draft_reply=data["draft_reply"],
                draft_reply_ko=data["draft_reply_ko"],
                used_sources=list(data["used_sources"]),
            )
        except (KeyError, TypeError) as e:
            raise DrafterError("로컬 AI 응답을 해석하지 못했어요.") from e
