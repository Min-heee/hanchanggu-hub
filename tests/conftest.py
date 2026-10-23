from datetime import date, datetime, time
from pathlib import Path

import pytest

from hub.app import build_hub

TODAY = date(2026, 10, 21)

# 개발자 .env에 넣은 실제 키·검증 값이 테스트에 섞이지 않게 한다 (웹훅 서명을 넣으면 시연 테스트가 401이 되는 등).
SECRET_ENV = ["ANTHROPIC_API_KEY", "INQUIRY_HUB_LLM", "CRM_ADAPTER", "NHN_WEBHOOK_SIGNATURE", "KAKAO_ADMIN_KEY",
              "NAVER_TALKTALK_WEBHOOK_TOKEN", "NAVER_TALKTALK_AUTH_KEY", "NAVER_TALKTALK_SEND_URL"]


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    import hub.app

    monkeypatch.setattr(hub.app, "load_env_file", lambda path: None)
    for name in SECRET_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(params=["sqlite", "csv"])
def hub(request, tmp_path: Path):
    """두 CRM 연결부 모두에서 같은 시나리오가 통과해야 한다."""
    h = build_hub(runtime=tmp_path, crm_kind=request.param, llm="offline", today=TODAY)
    # 날짜만 고정하고 시계를 실제 시간에 두면, 며칠 뒤 돌릴 때 인스타 답장 기한 같은 판단이 달라진다.
    h.clock = lambda: datetime.combine(TODAY, time(12, 0))
    h.reset()
    return h
