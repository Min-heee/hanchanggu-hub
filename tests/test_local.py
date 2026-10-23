"""로컬 AI(Ollama) 연결부. 진짜 Ollama 대신 같은 모양으로 답하는 가짜 서버를 띄워 확인한다."""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from hub.app import build_hub, build_recall
from hub.drafting import DrafterError
from hub.drafting.local import LocalDrafter, OllamaClient
from hub.recall.delivery import CHANNELS
from hub.recall.writer import LocalRecallWriter
from tests.conftest import TODAY


class FakeOllama:
    def __init__(self, status=200, content=None):
        self.status, self.content, self.requests = status, content, []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(request)
                content = fake.content(request) if callable(fake.content) else fake.content
                payload = {"message": {"role": "assistant", "content": json.dumps(content, ensure_ascii=False)}}
                if fake.status != 200:
                    payload = {"error": "model not found"}
                body = json.dumps(payload).encode()
                self.send_response(fake.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


ANALYSIS = {
    "category": "기타", "intent": "예약·일정", "urgency": "low", "language": "ko",
    "summary": "요약", "needs_human": False, "needs_human_reason": "",
    "draft_reply": "[고객1]님, 안녕하세요.", "draft_reply_ko": "[고객1]님, 안녕하세요.", "used_sources": [],
}


def test_request_uses_schema_and_turns_off_thinking():
    fake = FakeOllama(content=ANALYSIS)
    try:
        client = OllamaClient(model="qwen3:8b", base_url=fake.url)
        assert client.chat_json("system", "prompt", {"type": "object"}) == ANALYSIS
        sent = fake.requests[0]
        assert sent["model"] == "qwen3:8b" and sent["format"] == {"type": "object"}
        assert sent["think"] is False and sent["stream"] is False
        assert [m["role"] for m in sent["messages"]] == ["system", "user"]
    finally:
        fake.close()


def test_missing_model_says_how_to_pull_it():
    fake = FakeOllama(status=404)
    try:
        with pytest.raises(DrafterError, match="ollama pull qwen3:8b"):
            OllamaClient(model="qwen3:8b", base_url=fake.url).chat_json("s", "p", {})
    finally:
        fake.close()


def test_ollama_not_running_says_how_to_start_it():
    with socket.socket() as s:  # 아무도 듣지 않는 포트
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(DrafterError, match="ollama serve"):
        OllamaClient(base_url=f"http://127.0.0.1:{port}", timeout=2).chat_json("s", "p", {})


def test_local_mode_drafts_inbox_and_recall_through_ollama(tmp_path):
    fake = FakeOllama(content=ANALYSIS)
    try:
        hub = build_hub(runtime=tmp_path, crm_kind="sqlite", llm="local", today=TODAY)
        hub.drafter = LocalDrafter(OllamaClient(base_url=fake.url))
        hub.reset()
        view = hub.thread_view("cust:C002")
        assert view["mode"] == "local"
        assert view["analysis"]["draft_reply"].startswith("이민호님")  # 토큰이 실명으로 돌아온다
        assert all("이민호" not in r["messages"][1]["content"] for r in fake.requests)
        # 분류는 규칙 엔진이 정하고 모델은 문장만 쓴다 (모델 응답의 category '기타'는 무시)
        assert view["analysis"]["category"] == "눈성형" and view["analysis"]["intent"] == "회복·경과"
        assert all(r["think"] is True and "[분류 결과]" in r["messages"][1]["content"] for r in fake.requests)
        # 응급 문의는 모델을 부르지 않고 고정 문구로 나간다
        assert len(fake.requests) == 5
        urgent = hub.thread_view("cust:C004")
        assert "응급실" in urgent["analysis"]["draft_reply"]
        assert urgent["mode"] == "offline" and urgent["mode_label"] == "규칙 기반 초안"  # 화면이 '로컬 AI 초안'이라 하지 않게

        recall = build_recall(hub)
        assert isinstance(recall.writer, LocalRecallWriter)

        # 실제 실행에서 qwen3가 한 것처럼, 지시와 달리 머리줄과 '무료'가 빠진 수신거부 줄까지 써 오는 모델
        fake.content = lambda request: {
            "message": "(광고) 데모성형외과\n\n[고객1]님, 안부 인사드려요. 대표번호나 카카오톡 채널 채팅으로 편하게 연락 주세요.\n\n수신거부 080-000-0000",
            "notes": "", "used_sources": [],
        }
        recall.plan(recall.today)
        items = {i["customer_name"]: i for i in recall.items(recall.today.isoformat()) if i["mode"] == "local"}
        # 한도윤은 카카오 채널 친구가 아니라 문자(080 수신거부), 오세린은 친구라 브랜드 메시지(채널 차단)
        assert items["한도윤"]["channel"] == "sms" and items["오세린"]["channel"] == "friendtalk"
        for item in items.values():
            assert "[광고 표시]" in item["prompt"] and f"[발송 채널] {CHANNELS[item['channel']].label}" in item["prompt"]
            # 법정 표시는 코드가 붙인다: 모델이 쓴 머리줄·수신거부 줄은 떼고 정해진 문구로 바꾼다
            assert item["text"].startswith("(광고) 데모성형외과 대표번호 02-000-0000\n\n")
            assert item["text"].endswith("\n\n" + CHANNELS[item["channel"]].opt_out)
            assert item["text"].count("수신거부") == 1
            assert f"{item['customer_name']}님" in item["text"] and item["flags"] == []
    finally:
        fake.close()
