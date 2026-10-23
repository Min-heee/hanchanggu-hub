"""네이버 톡톡 연동. 진짜 네이버 대신 보내기 API와 같은 모양으로 답하는 가짜 서버를 띄워 확인한다.

웹훅 예시는 github.com/navertalk/chatbot-api README와 이슈에 실린 실제 페이로드 모양을 따랐다.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from hub.app import build_recall, create_app, create_webhook_app
from hub.talktalk import TalkTalkClient, TalkTalkError, parse_agent_echo, parse_inbound

TOKEN = "s3cret-path"
USER = "8ZabnI9f_WzRGNtkmsegTA"


class FakeTalkTalk:
    def __init__(self, reply=None):
        self.reply = reply or {"success": True, "resultCode": "00"}
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                data = json.dumps(fake.reply).encode()
                self.send_response(200)  # 네이버는 오류도 200에 success:false로 준다
                self.send_header("Content-Type", "application/json;charset=UTF-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/chatbot/v1/event"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def fake():
    server = FakeTalkTalk()
    yield server
    server.close()


@pytest.fixture
def client(hub, fake, monkeypatch):
    monkeypatch.setenv("NAVER_TALKTALK_WEBHOOK_TOKEN", TOKEN)
    hub.channels.talktalk = TalkTalkClient("issued-key-123", url=fake.url)
    return TestClient(create_app(hub, build_recall(hub)))


def send_event(text, user=USER, message_id=4):
    return {"event": "send", "user": user, "messageId": message_id,
            "textContent": {"text": text, "inputType": "typing"}, "options": {"mobile": False}}


def talktalk_thread(hub, user=USER):
    return next(t for t in hub.store.threads() if t["key"] == f"naver:{user}")


# ---- 웹훅 페이로드 읽기 -----------------------------------------------------------

def test_parse_inbound_reads_text_buttons_and_images():
    msg = parse_inbound(send_event("내일 오후 상담 예약 가능한가요?"), "2026-10-21T14:00")
    assert (msg.id, msg.channel, msg.channel_user_id, msg.received_at) == (f"talktalk:{USER}:4", "naver", USER, "2026-10-21T14:00")
    assert msg.text == "내일 오후 상담 예약 가능한가요?"

    button = {"event": "send", "user": USER, "textContent": {"text": "예약 문의", "code": "BOOKING", "inputType": "button"}}
    assert parse_inbound(button, "2026-10-21T14:00").text == "예약 문의"
    image = {"event": "send", "user": USER, "messageId": "76",  # messageId가 문자열로 올 때도 있다
             "imageContent": {"imageUrl": "https://shop-phinf.pstatic.net/a.jpg", "width": "1080", "height": "1022"}}
    parsed = parse_inbound(image, "2026-10-21T14:00")
    assert parsed.id == f"talktalk:{USER}:76" and parsed.text == "[사진을 보냈어요] https://shop-phinf.pstatic.net/a.jpg"
    # 파일과 함께 빈 textContent가 올 수 있다
    with_file = {"event": "send", "user": USER, "textContent": {}, "fileContent": {"fileName": "진단서.pdf"}}
    assert parse_inbound(with_file, "2026-10-21T14:00").text == "[파일을 보냈어요] 진단서.pdf"


@pytest.mark.parametrize("payload", [
    {"event": "open", "user": USER, "options": {"inflow": "none", "under14": False}},
    {"event": "leave", "user": USER, "options": {"mobile": False}},
    {"event": "friend", "user": USER, "options": {"set": "on"}},
    {"event": "send", "user": USER, "textContent": {}},
    {"event": "send", "textContent": {"text": "user 없음"}},
])
def test_other_events_are_not_inquiries(payload):
    assert parse_inbound(payload, "2026-10-21T14:00") is None


def test_echo_only_counts_replies_typed_by_an_agent():
    typed = {"echoedEvent": "send", "event": "echo", "user": USER, "textContent": {"text": "확인해 드릴게요", "inputType": "typing"},
             "options": {"mobile": False, "managerNickname": "박하은"}}
    assert parse_agent_echo(typed).manager == "박하은"
    ours = {**typed, "options": {"mobile": False}}  # 보내기 API로 보낸 우리 답장의 echo
    assert parse_agent_echo(ours) is None


# ---- 웹훅 받기 ------------------------------------------------------------------

def test_webhook_is_off_without_a_token(hub, monkeypatch):
    monkeypatch.delenv("NAVER_TALKTALK_WEBHOOK_TOKEN", raising=False)
    client = TestClient(create_app(hub, build_recall(hub)))
    assert client.post("/webhooks/naver-talktalk/anything", json=send_event("안녕하세요")).status_code == 404


def test_webhook_adds_an_inquiry_and_always_answers_200(client, hub):
    assert client.post("/webhooks/naver-talktalk/wrong", json=send_event("안녕하세요")).status_code == 404

    res = client.post(f"/webhooks/naver-talktalk/{TOKEN}", json=send_event("코 수술 상담 받고 싶어요. 토요일도 되나요?"))
    assert res.status_code == 200 and res.content == b""
    client.post(f"/webhooks/naver-talktalk/{TOKEN}", json=send_event("코 수술 상담 받고 싶어요. 토요일도 되나요?"))  # 같은 messageId
    thread = talktalk_thread(hub)
    assert thread["display_name"] == "네이버 톡톡 사용자" and thread["customer_id"] is None
    assert len(hub.store.messages(thread["key"])) == 1

    # 처리할 수 없는 본문이어도 200 (네이버는 200이 아니면 실패로 쌓다가 웹훅을 막는다)
    assert client.post(f"/webhooks/naver-talktalk/{TOKEN}", content=b"not json").status_code == 200
    assert client.post(f"/webhooks/naver-talktalk/{TOKEN}", json={"event": "open", "user": USER}).status_code == 200


def test_reply_goes_out_through_the_send_api(client, hub, fake):
    client.post(f"/webhooks/naver-talktalk/{TOKEN}", json=send_event("상담 예약하고 싶어요"))
    key = talktalk_thread(hub)["key"]
    hub.analyze(key)
    res = client.post(f"/api/threads/{key}/approve", json={"text": "안녕하세요, 데모성형외과입니다.\n상담 가능한 시간을 안내드릴게요."})
    assert res.status_code == 200 and res.json()["thread"]["status"] == "sent"

    request = fake.requests[-1]
    assert request["path"] == "/chatbot/v1/event"
    assert request["headers"]["Authorization"] == "issued-key-123"  # 발급 키 그대로 (Bearer 없음)
    assert request["headers"]["Content-Type"] == "application/json;charset=UTF-8"
    assert request["body"] == {"event": "send", "user": USER,
                               "textContent": {"text": "안녕하세요, 데모성형외과입니다.\n상담 가능한 시간을 안내드릴게요."},
                               "options": {"notification": True}}  # partner 필드는 넣지 않는다
    last = json.loads(hub.channels.outbox_path.read_text(encoding="utf-8").splitlines()[-1])
    assert last["via"] == "naver_talktalk_api" and last["to"] == USER


def test_demo_threads_never_hit_the_real_api(client, hub, fake):
    view = client.get("/api/threads/cust:C002").json()  # 시드의 네이버 톡톡 문의(이민호)
    client.post("/api/threads/cust:C002/approve", json={"text": view["analysis"]["draft_reply"]})
    assert fake.requests == []


@pytest.mark.parametrize("reply, expected", [
    ({"success": False, "resultCode": "01", "resultMessage": "권한없는 파트너에 대한 요청"}, "테스트 계정의 고객에게 실서비스 키"),
    ({"success": False, "resultCode": "99", "resultMessage": "잘못된 user 값 : abc"}, "다른 파트너 계정"),
    ({"success": False, "resultCode": "99", "resultMessage": "알 수 없는 오류"}, "파트너센터에서 직접"),
    ({"success": False, "resultCode": "01", "resultMessage": "현재 이 사용자에게 메시지를 보낼 수 없음"}, "1~2분 뒤"),
])
def test_send_failures_keep_the_thread_open(client, hub, fake, reply, expected):
    fake.reply = reply
    client.post(f"/webhooks/naver-talktalk/{TOKEN}", json=send_event("상담 예약하고 싶어요"))
    key = talktalk_thread(hub)["key"]
    hub.analyze(key)
    res = client.post(f"/api/threads/{key}/approve", json={"text": "답장"})
    assert res.status_code == 400 and expected in res.json()["detail"]
    assert hub.store.thread(key)["status"] == "drafted"  # 보내지 못했으니 '보냄'으로 바꾸지 않는다
    assert hub.crm.find_by_channel("naver", USER) is None  # 신규 리드도 만들지 않는다


def test_missing_send_key_is_explained(client, hub):
    hub.channels.talktalk = None
    client.post(f"/webhooks/naver-talktalk/{TOKEN}", json=send_event("상담 예약하고 싶어요"))
    key = talktalk_thread(hub)["key"]
    hub.analyze(key)
    res = client.post(f"/api/threads/{key}/approve", json={"text": "답장"})
    assert res.status_code == 400 and "NAVER_TALKTALK_AUTH_KEY" in res.json()["detail"]


def test_client_rejects_overlong_text():
    with pytest.raises(TalkTalkError, match="10,000자"):
        TalkTalkClient("k", url="http://127.0.0.1:9").send_text(USER, "가" * 10_001)


def test_agent_reply_in_partner_center_closes_the_thread(client, hub):
    client.post(f"/webhooks/naver-talktalk/{TOKEN}", json=send_event("내일 실밥 풀러 가도 되나요?", user="nt_minho", message_id=9))
    key = "cust:C002"
    assert hub.store.thread(key)["status"] in ("analyzing", "drafted", "new")
    echo = {"echoedEvent": "send", "event": "echo", "user": "nt_minho",
            "textContent": {"text": "네, 예약하신 시간에 오시면 됩니다.", "inputType": "typing"},
            "options": {"mobile": False, "managerNickname": "박하은"}}
    client.post(f"/webhooks/naver-talktalk/{TOKEN}", json=echo)
    hub._executor.shutdown(wait=True)  # 분석이 끝난 뒤에도 상태가 유지되는지 본다
    thread = hub.store.thread(key)
    assert thread["status"] == "sent" and "박하은" in thread["note"]
    assert hub.crm.contact_logs("C002")[-1].handled_by == "박하은"


def test_opt_out_over_talktalk_reaches_the_crm(client, hub):
    client.post(f"/webhooks/naver-talktalk/{TOKEN}", json=send_event("광고 문자는 수신거부할게요", user="nt_doyoon", message_id=11))
    assert hub.crm.get("C007").marketing_consent is False
    assert hub.crm.consent_history("C007")[-1].source == "네이버 톡톡 답장 '수신거부'"


# ---- 인터넷에 여는 웹훅 전용 앱 ------------------------------------------------------

def test_webhook_app_exposes_only_webhooks(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVER_TALKTALK_WEBHOOK_TOKEN", TOKEN)
    import hub.app as app_module

    real_build_hub = app_module.build_hub
    monkeypatch.setattr(app_module, "build_hub", lambda: real_build_hub(runtime=tmp_path, llm="offline"))
    client = TestClient(create_webhook_app())
    assert client.get("/api/threads").status_code == 404
    assert client.get("/").status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.post(f"/webhooks/naver-talktalk/{TOKEN}", json=send_event("안녕하세요")).status_code == 200
    assert client.post("/api/demo/webhooks/nhn-sms", json={}).status_code == 404  # 시연 경로는 본 앱에만

    # 인터넷에 열린 앱은 검증 값이 없으면 수신거부 웹훅을 받지 않는다 (아무나 동의를 끄거나 번호로 이름을 알아내지 못하게)
    unsubscribe = {"event": "UNSUBSCRIBE", "hooks": [{"recipientNo": "01024681357", "createdDateTime": "2026-10-21T11:25:10.000+09:00"}]}
    assert client.post("/webhooks/nhn-sms", json=unsubscribe).status_code == 404
    assert client.post("/webhooks/kakao-channel", json={"event": "blocked", "id": "k_serin", "updated_at": "2026-10-21T02:00:00Z"}).status_code == 404
    monkeypatch.setenv("NHN_WEBHOOK_SIGNATURE", "서명-값")  # 한글 같은 비ASCII 값도 예외 없이 비교한다
    assert client.post("/webhooks/nhn-sms", json=unsubscribe, headers={"X-Toast-Webhook-Signature": "wrong"}).status_code == 401
    ok = client.post("/webhooks/nhn-sms", json=unsubscribe,
                     headers={"X-Toast-Webhook-Signature": "서명-값".encode()})
    assert ok.status_code == 200 and ok.json() == {"results": [{"changed": True, "stopped": 0}]}  # 이름·고객 ID는 돌려주지 않는다
