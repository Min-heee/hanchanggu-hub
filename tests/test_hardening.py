"""여러 에이전트 검토에서 확인된 문제를 고친 뒤, 같은 문제가 다시 생기지 않는지 지키는 테스트."""
import threading
from datetime import datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient

from hub.app import build_hub, build_recall, create_app
from hub.crm import CsvCRM
from hub.drafting.rules import RuleDrafter
from hub.models import ConsentEvent, InboundMessage
from hub.optout import opt_out_intent
from hub.pipeline import StaleThread
from hub.talktalk import TalkTalkClient, TalkTalkError
from tests.conftest import TODAY
from tests.test_recall import change_customers
from tests.test_talktalk import FakeTalkTalk


@pytest.fixture
def recall(hub):
    r = build_recall(hub)
    r.clock = lambda: datetime.combine(r.today, time(11, 0))
    return r


def item(recall, name, rule_name, offset=0):
    day = (recall.today + timedelta(days=offset)).isoformat()
    return next(i for i in recall.items(day) if i["customer_name"] == name and i["rule_name"] == rule_name)


AD = ("한도윤", "상담 후 미예약 재상담 안내")


# ---- 리콜: 수신거부가 어느 순간에 들어와도 광고가 나가지 않는다 ------------------------

def test_opt_out_also_stops_items_that_failed_to_draft(hub, recall):
    recall.plan(recall.today)
    ad = item(recall, *AD)
    recall.store.update(ad["id"], status="error", note="메시지를 준비하지 못했어요")
    assert hub.optout.from_080(hub.crm.get("C007").phone).stopped == 1
    assert recall.store.get(ad["id"])["status"] == "blocked"


def test_approve_rechecks_consent_even_if_the_listener_missed_it(hub, recall):
    recall.plan(recall.today)
    ad = item(recall, *AD)
    change_customers(recall, C007={"marketing_consent": False})  # CRM에서 바로 바뀌어 리스너가 모르는 경우
    with pytest.raises(ValueError, match="보내지 않았어요"):
        recall.approve(ad["id"])
    assert recall.store.get(ad["id"])["status"] == "blocked"
    assert not recall.channels.outbox_path.exists()


def test_a_message_is_claimed_once(hub, recall):
    recall.plan(recall.today)
    ad = item(recall, *AD)
    recall.store.update(ad["id"], status="sending")  # 다른 화면이 이미 집었다
    with pytest.raises(ValueError, match="상태"):
        recall.approve(ad["id"])
    recall.clock = lambda: datetime.combine(recall.today, time(22, 0))
    other = item(recall, "오세린", "쁘띠 시술 3개월 안부")
    recall.approve(other["id"])  # 밤 → 다음 날 10시 예약
    recall.store.update(other["id"], status="sending")
    recall.clock = lambda: datetime.combine(recall.today + timedelta(days=1), time(10, 5))
    assert recall.dispatch_due() == 0  # 이미 집힌 예약은 다시 보내지 않는다


def test_two_year_check_stops_after_withdrawal(hub, recall):
    recall.plan(recall.today)
    check = item(recall, "김지수", "광고 수신동의 2년 확인")
    assert hub.optout.from_080(hub.crm.get("C001").phone).stopped == 1
    assert recall.store.get(check["id"])["status"] == "blocked"


def test_missing_consent_date_is_shown_not_skipped(hub, recall):
    change_customers(recall, consents={"C009": []})  # 동의는 '예'인데 날짜 기록이 없다
    recall.plan(recall.today)
    held = item(recall, "오세린", "광고 수신동의 2년 확인")
    assert held["status"] == "blocked" and "동의 날짜 기록이 없어" in held["block_reason"]


def test_080_applies_to_every_record_with_that_number(hub):
    lead = hub.crm.create_lead(name="한도윤 어머니", phone="010-2468-1357", email="", language="ko", channel="web", channel_user_id="w1")
    hub.crm.update_consent(lead.id, ConsentEvent(at="2026-10-20T09:00", field="marketing_consent", value=True, source="상담 동의서"))
    result = hub.optout.from_080("01024681357")
    assert result.changed
    assert hub.crm.get("C007").marketing_consent is False and hub.crm.get(lead.id).marketing_consent is False


# ---- 문의함: 수신거부 말을 정확히 알아듣는다 ---------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("광고 수신동의 철회합니다", "request"),
    ("광고 문자 보내지 마세요", "request"),
    ("이벤트 메시지는 안 받을게요", "request"),
    ("수신거부 해제해 주세요. 이벤트 소식 받고 싶어요", "unclear"),
    ("수신거부 안 했는데요? 왜 문자가 안 와요", "unclear"),
    ("수신거부는 어떻게 하나요?", "unclear"),
    ("이벤트 가격 아직 그대로인가요?", None),
])
def test_opt_out_intent(text, expected):
    assert opt_out_intent(text) == expected


def thread_of(hub, customer_id):
    return next(t["key"] for t in hub.store.threads() if t["customer_id"] == customer_id)


def test_unclear_opt_out_is_flagged_not_applied(hub):
    hub.ingest([InboundMessage(id="u1", channel="naver", channel_user_id="nt_doyoon", sender_name="한도윤",
                               text="수신거부 해제하고 싶어요. 이벤트 소식 다시 받을래요", received_at="2026-10-21T11:30")])
    assert hub.crm.get("C007").marketing_consent is True
    view = hub.thread_view(thread_of(hub, "C007"))
    assert any(f["phrase"] == "수신거부 관련 문의" for f in view["flags"])
    assert "거부 처리했어요" not in view["analysis"]["draft_reply"]


def test_opt_out_from_unknown_sender_is_flagged(hub):
    hub.ingest([InboundMessage(id="u2", channel="naver", channel_user_id="nt_new", sender_name="익명",
                               text="광고 문자 보내지 마세요", received_at="2026-10-21T11:30")])
    view = hub.thread_view("naver:nt_new")
    assert any(f["phrase"] == "수신거부 요청 · CRM 고객 못 찾음" for f in view["flags"])


def test_notice_uses_the_date_the_refusal_took_effect(hub):
    hub.optout.from_080(hub.crm.get("C009").phone, at="2026-10-15T10:00")  # 먼저 080으로 거부
    hub.sync()
    hub.sync()  # 21일에 '수신거부 해주세요' 답장
    view = hub.thread_view(thread_of(hub, "C009"))
    assert "10월 15일 자로" in view["analysis"]["draft_reply"]
    assert any("이미 10/15" in f["reason"] for f in view["flags"] if f["phrase"] == "수신거부 요청")


def test_staff_can_apply_an_opt_out(hub, recall):
    client = TestClient(create_app(hub, recall))
    res = client.post("/api/customers/C007/opt-out", json={"handled_by": "박하은 실장"}).json()
    assert res["changed"] and hub.crm.consent_history("C007")[-1].source == "상담실장 확인 (박하은 실장)"
    assert client.post("/api/customers/NOPE/opt-out", json={}).status_code == 404


# ---- 문의함: 같은 문의에 두 번 답하지 않는다 ---------------------------------------

def test_approve_refuses_a_thread_answered_elsewhere(hub):
    key = "cust:C002"
    hub.record_agent_reply("naver", "nt_minho", "네, 오시면 됩니다.", "박하은")
    with pytest.raises(StaleThread, match="이미 답장한"):
        hub.approve(key, "답장")


def test_reply_stamped_earlier_than_the_question_still_counts(hub):
    # 문의 시각이 답장 시각보다 늦게 찍혀 있어도(시계 차이, 시드 데이터) 답장 뒤에 들어온 게 아니면 이미 답한 것이다
    hub.record_agent_reply("naver", "nt_minho", "네, 오시면 됩니다.", "박하은", at="2026-10-21T08:00")
    with pytest.raises(StaleThread, match="이미 답장한"):
        hub.approve("cust:C002", "답장")


def test_approve_refuses_when_a_new_message_arrived(hub):
    key = "cust:C002"
    seen = [m for m in hub.store.messages(key) if m["direction"] == "in"][-1]["id"]
    hub.ingest([InboundMessage(id="n1", channel="naver", channel_user_id="nt_minho", sender_name="이민호",
                               text="아 그리고 주차는 되나요?", received_at="2026-10-21T11:59")])
    with pytest.raises(StaleThread, match="새 메시지"):
        hub.approve(key, "답장", last_seen=seen)


def test_api_returns_409_for_stale_approve(hub, recall):
    client = TestClient(create_app(hub, recall))
    view = client.get("/api/threads/cust:C002").json()
    seen = [m for m in view["messages"] if m["direction"] == "in"][-1]["id"]
    assert client.post("/api/threads/cust:C002/approve", json={"text": "답장", "last_seen": seen}).status_code == 200
    assert client.post("/api/threads/cust:C002/approve", json={"text": "답장", "last_seen": seen}).status_code == 409


def test_same_minute_question_after_reply_stays_open(hub):
    key = "cust:C002"
    hub.record_agent_reply("naver", "nt_minho", "네, 오시면 됩니다.", "박하은", at="2026-10-21T14:05")
    hub.ingest([InboundMessage(id="n2", channel="naver", channel_user_id="nt_minho", sender_name="이민호",
                               text="몇 시까지 가면 될까요?", received_at="2026-10-21T14:05")])
    assert [m["text"] for m in hub._pending(hub.store.messages(key))] == ["몇 시까지 가면 될까요?"]
    assert hub.store.thread(key)["status"] == "drafted"


def test_late_draft_does_not_reopen_a_thread_answered_meanwhile(tmp_path):
    release = threading.Event()

    class SlowDrafter(RuleDrafter):
        def draft(self, request):
            release.wait(5)
            return super().draft(request)

    hub = build_hub(runtime=tmp_path, llm="offline", today=TODAY)
    hub.clock = lambda: datetime.combine(TODAY, time(12, 0))
    hub.reset()
    hub.drafter = SlowDrafter()
    hub.ingest([InboundMessage(id="s1", channel="naver", channel_user_id="nt_minho", sender_name="이민호",
                               text="내일 실밥 풀러 가도 되나요?", received_at="2026-10-21T11:50")], wait_for_drafts=False)
    hub.record_agent_reply("naver", "nt_minho", "네, 오시면 됩니다.", "박하은")  # 초안을 쓰는 사이 파트너센터에서 답함
    release.set()
    hub._executor.shutdown(wait=True)
    thread = hub.store.thread("cust:C002")
    assert thread["status"] == "sent" and "박하은" in thread["note"]


def test_duplicate_webhook_after_reply_does_not_reopen(hub):
    msg = InboundMessage(id="d1", channel="naver", channel_user_id="nt_minho", sender_name="이민호",
                         text="감사합니다", received_at="2026-10-21T11:55")
    hub.ingest([msg])
    hub.approve("cust:C002", "네, 감사합니다.")
    hub.ingest([msg])
    hub.store.add_inbound("cust:C002", msg, "C002", "이민호")  # 저장소에 바로 넣어도 다시 열지 않는다
    assert hub.store.thread("cust:C002")["status"] == "sent"


# ---- 운영: 날짜, 다시 켜기, 두 프로세스 -----------------------------------------------

def test_today_follows_the_clock_when_not_pinned(tmp_path):
    hub = build_hub(runtime=tmp_path, llm="offline")
    hub.clock = lambda: datetime(2026, 10, 24, 0, 5)
    assert hub.today.isoformat() == "2026-10-24"
    recall = build_recall(hub)
    recall.clock = hub.clock
    assert recall.today.isoformat() == "2026-10-24"


def test_drafts_interrupted_by_a_restart_are_resumed(hub):
    key = "cust:C002"
    hub.store.set_status(key, "analyzing")
    hub.ensure_seeded()
    hub._executor.shutdown(wait=True)
    assert hub.store.thread(key)["status"] == "drafted"


def test_csv_crm_sees_changes_from_another_process(tmp_path):
    hub = build_hub(runtime=tmp_path, crm_kind="csv", llm="offline", today=TODAY)
    hub.reset()
    other = CsvCRM(tmp_path / "csv_crm")  # 웹훅 전용 앱의 CRM
    other.update_consent("C007", ConsentEvent(at="2026-10-21T13:00", field="marketing_consent", value=False, source="080 수신거부"))
    assert hub.crm.get("C007").marketing_consent is False
    assert hub.crm.consent_history("C007")[-1].source == "080 수신거부"


def test_other_sites_cannot_post_to_the_local_app(hub, recall):
    client = TestClient(create_app(hub, recall))
    assert client.post("/api/reset", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/reset", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post("/api/sync", headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert client.get("/api/status", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200  # 읽기는 막지 않는다


def test_send_with_unknown_outcome_is_not_reported_as_sent():
    fake = FakeTalkTalk(reply=["unexpected"])
    try:
        with pytest.raises(TalkTalkError, match="보냈는지 알 수 없어요"):
            TalkTalkClient("k", url=fake.url).send_text("user", "안녕하세요")
    finally:
        fake.close()
