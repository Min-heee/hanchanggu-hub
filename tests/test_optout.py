"""수신거부(080·카카오 채널 차단·문의 답장)가 CRM 동의와 이력, 준비 중인 광고에 바로 반영되는지."""
from datetime import datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient

from hub.app import build_recall, create_app
from hub.models import InboundMessage
from hub.optout import is_opt_out_request


@pytest.fixture
def recall(hub):
    r = build_recall(hub)
    r.clock = lambda: datetime.combine(r.today, time(11, 0))
    return r


def items_by_name(recall, offset=0):
    return {(i["customer_name"], i["rule_name"]): i for i in recall.items((recall.today + timedelta(days=offset)).isoformat())}


def test_080_opt_out_turns_off_ad_consent_and_keeps_history(hub):
    phone = hub.crm.get("C007").phone
    result = hub.optout.from_080(phone, at="2026-10-21T14:05")
    assert result.changed and result.customer_id == "C007"

    customer = hub.crm.get("C007")
    assert customer.marketing_consent is False
    history = hub.crm.consent_history("C007")
    assert [(e.field, e.value, e.source) for e in history] == [
        ("marketing_consent", True, "상담 동의서"),
        ("marketing_consent", False, "080 수신거부"),
    ]
    assert history[-1].at == "2026-10-21T14:05"
    assert hub.crm.contact_logs("C007")[-1].channel == "동의 변경"

    again = hub.optout.from_080(phone)  # 대행사가 같은 번호를 다시 알려 줘도 이력이 늘지 않는다
    assert not again.changed and len(hub.crm.consent_history("C007")) == 2


def test_080_number_not_in_crm_changes_nothing(hub):
    result = hub.optout.from_080("010-1111-0000")
    assert result.customer_id is None and not result.changed


def test_kakao_channel_block_counts_as_refusing_all_ads(hub):
    blocked = hub.optout.from_kakao("k_serin", blocked=True, at="2026-10-21T10:30")
    customer = hub.crm.get("C009")
    assert blocked.changed and customer.kakao_friend is False
    # 범위를 밝히지 않은 수신거부는 광고 전체 거부로 본다 (KISA 해설). 문자 광고로 돌려 보내지 않는다.
    assert customer.marketing_consent is False

    hub.optout.from_kakao("k_serin", blocked=False)
    customer = hub.crm.get("C009")
    assert customer.kakao_friend is True and customer.marketing_consent is False  # 채널 추가는 광고 동의가 아니다
    assert [(e.field, e.value, e.source) for e in hub.crm.consent_history("C009")][-3:] == [
        ("kakao_friend", False, "카카오 채널 차단"),
        ("marketing_consent", False, "카카오 채널 차단"),
        ("kakao_friend", True, "카카오 채널 추가"),
    ]


def test_kakao_user_not_linked_in_crm_changes_nothing(hub):
    result = hub.optout.from_kakao("unknown_user", blocked=True)
    assert result.customer_id is None and not result.changed


@pytest.mark.parametrize("text, expected", [
    ("수신거부 해주세요", True),
    ("광고 문자 이제 그만 보내지 마세요", True),
    ("앞으로 광고 받지 않을게요", True),
    ("Please unsubscribe me", True),
    ("붓기가 언제 빠지는지 궁금해요", False),
    ("When can I stop wearing the splint?", False),  # 영어 STOP은 한국 수신거부 방식이 아니고, 질문에 흔히 나온다
])
def test_opt_out_words(text, expected):
    assert is_opt_out_request(text) is expected


def test_opt_out_reply_in_inbox_updates_crm_and_draft(hub):
    hub.sync()  # batch 2
    hub.sync()  # batch 3: 오세린의 수신거부 답장
    customer = hub.crm.get("C009")
    assert customer.marketing_consent is False and customer.kakao_friend is True
    assert hub.crm.consent_history("C009")[-1].source == "카카오톡 답장 '수신거부'"

    key = next(t["key"] for t in hub.store.threads() if t["customer_id"] == "C009")
    view = hub.thread_view(key)
    draft = view["analysis"]["draft_reply"]
    # 인사 다음 문단이 처리 결과 안내다 (보낸 곳·처리 날짜·처리 내용)
    assert draft.split("\n\n")[1].startswith("요청하신 대로 10월 21일 자로 데모성형외과의 광고성 정보 수신을 거부 처리했어요.")
    assert any(f["phrase"] == "수신거부 요청" for f in view["flags"])
    assert view["consent_history"][-1]["value"] is False


def test_opt_out_from_unknown_sender_is_safe(hub):
    msg = InboundMessage(id="x1", channel="naver", channel_user_id="nt_new", sender_name="익명",
                         text="광고 문자 수신거부요", received_at="2026-10-21T11:30")
    hub.ingest([msg])
    key = next(t["key"] for t in hub.thread_list() if t["name"] == "익명")
    view = hub.thread_view(key)
    assert view["consent_history"] == []
    assert "거부 처리했어요" not in view["analysis"]["draft_reply"]


def test_webhook_retries_do_not_duplicate_messages(hub):
    msg = InboundMessage(id="x2", channel="naver", channel_user_id="nt_minho", sender_name="이민호",
                         text="내일 몇 시에 가면 되나요?", received_at="2026-10-21T11:40")
    hub.ingest([msg])
    hub.ingest([msg])
    key = next(t["key"] for t in hub.store.threads() if t["customer_id"] == "C002")
    assert sum(1 for m in hub.thread_view(key)["messages"] if m["text"] == msg.text) == 1


# ---- 리콜과 이어지는지 --------------------------------------------------------

def test_080_opt_out_stops_a_waiting_ad_right_away(hub, recall):
    recall.plan(recall.today)
    ad = items_by_name(recall)[("한도윤", "상담 후 미예약 재상담 안내")]
    assert ad["status"] == "ready"

    result = hub.optout.from_080(hub.crm.get("C007").phone, at="2026-10-21T11:00")
    assert result.stopped == 1 and "1건을 멈췄어요" in result.message
    stopped = recall.store.get(ad["id"])
    assert stopped["status"] == "blocked"
    assert stopped["block_reason"] == "수신거부 반영: 080 수신거부 (11:00)"
    with pytest.raises(ValueError):
        recall.approve(ad["id"])


def test_opt_out_stops_a_scheduled_ad_but_not_info_messages(hub, recall):
    recall.clock = lambda: datetime.combine(recall.today, time(22, 0))  # 밤에 승인 → 다음 날 10시 예약
    recall.plan(recall.today)
    items = items_by_name(recall)
    ad = items[("오세린", "쁘띠 시술 3개월 안부")]
    recall.approve(ad["id"])
    assert recall.store.get(ad["id"])["status"] == "scheduled"
    info = [i for i in items.values() if i["kind"] == "정보성" and i["status"] == "ready"]

    hub.optout.from_080(hub.crm.get("C009").phone)
    assert recall.store.get(ad["id"])["status"] == "blocked"
    assert all(recall.store.get(i["id"])["status"] == "ready" for i in info)


def test_channel_block_stops_the_ad_and_queues_the_result_notice(hub, recall):
    recall.plan(recall.today)
    brand = items_by_name(recall)[("오세린", "쁘띠 시술 3개월 안부")]
    assert brand["channel"] == "friendtalk"

    result = hub.optout.from_kakao("k_serin", blocked=True, at="2026-10-21T10:30")
    assert result.stopped == 1
    assert recall.store.get(brand["id"])["block_reason"] == "수신거부 반영: 카카오 채널 차단 (10:30)"

    recall.plan(recall.today)
    items = items_by_name(recall)
    ad = items[("오세린", "쁘띠 시술 3개월 안부")]
    assert ad["status"] == "blocked" and ad["channel"] == "sms"  # 문자로 돌려 보내지 않는다
    assert ad["block_reason"] == "수신거부 반영: 카카오 채널 차단 (10/21). 광고는 보내지 않아요."  # 다시 계획해도 이유가 남는다
    notice = items[("오세린", "수신 동의·거부 처리 결과 안내")]
    assert notice["status"] == "ready" and notice["channel"] == "alimtalk"
    # 법정 항목: 보낸 곳 이름, 거부 사실과 날짜, 처리 결과. 광고 내용·(광고) 표시는 넣지 않는다.
    assert notice["text"] == ("[데모성형외과] 광고성 정보 수신거부 처리 결과 안내\n\n"
                              "오세린님이 10월 21일에 요청하신 광고성 정보 수신거부를 10월 21일에 처리했습니다.\n\n"
                              "문의: 대표번호 02-000-0000")
    assert "11월 4일까지" in notice["reason"]  # 다음 날부터 14일


def test_result_notice_deadline_window(hub, recall):
    hub.optout.from_080(hub.crm.get("C007").phone, at="2026-10-07T15:00")  # 14일 전
    recall.plan(recall.today)
    assert ("한도윤", "수신 동의·거부 처리 결과 안내") in items_by_name(recall)
    later = recall.today + timedelta(days=1)  # 15일째에는 이미 기한이 지났다
    recall.plan(later)
    assert ("한도윤", "수신 동의·거부 처리 결과 안내") not in items_by_name(recall, 1)


def test_reply_that_carried_the_notice_replaces_the_separate_one(hub, recall):
    hub.sync()
    hub.sync()  # 오세린의 '수신거부' 답장 → 초안 첫머리에 처리 결과 안내
    recall.plan(recall.today)
    # 답장을 못 보낼 수도 있으니 막지 않고, 겹친다는 표시만 한다
    notice = items_by_name(recall)[("오세린", "수신 동의·거부 처리 결과 안내")]
    assert notice["status"] == "ready" and any(f["phrase"] == "문의함 답장과 겹침" for f in notice["flags"])

    key = next(t["key"] for t in hub.store.threads() if t["customer_id"] == "C009")
    hub.approve(key, hub.thread_view(key)["analysis"]["draft_reply"])
    with pytest.raises(ValueError, match="이미 알렸어요"):  # 답장이 먼저 나갔으면 알림톡은 보내지 않는다
        recall.approve(notice["id"])
    assert recall.store.get(notice["id"])["status"] == "blocked"
    recall.plan(recall.today)  # 다시 계획해도 새로 만들지 않는다
    assert not [i for i in recall.items(recall.today.isoformat())
                if i["rule_id"] == "consent_result_notice" and i["status"] in ("ready", "sent", "scheduled")]


def test_two_year_consent_check(hub, recall):
    recall.plan(recall.today)
    check = items_by_name(recall)[("김지수", "광고 수신동의 2년 확인")]
    assert check["kind"] == "정보성" and check["channel"] == "alimtalk"
    assert check["text"].startswith("[데모성형외과] 광고성 정보 수신동의 확인 안내\n\n"
                                    "김지수님은 2024년 10월 23일에 데모성형외과의 광고성 정보 수신에 동의하셨습니다(동의 경로: 홈페이지 회원가입).")
    assert check["text"].endswith("무료수신거부 080-000-0000")
    assert "(광고)" not in check["text"]
    assert "10월 22일까지" in check["reason"]

    # 승인하지 못하면 기한까지 다음 날 목록으로 넘어온다 (새로 만들지 않고 같은 항목)
    recall.plan(recall.today + timedelta(days=1))
    assert items_by_name(recall, 1)[("김지수", "광고 수신동의 2년 확인")]["id"] == check["id"]
    # 보내고 나면 같은 주기에는 다시 만들지 않는다. 다음 확인은 그로부터 2년 뒤다.
    recall.approve(check["id"])
    recall.plan(recall.today + timedelta(days=1))
    assert [i["status"] for i in recall.items((recall.today + timedelta(days=1)).isoformat())
            if i["rule_id"] == "consent_reconfirm"] == ["sent"]


def test_two_year_check_skips_customers_who_withdrew(hub, recall):
    hub.optout.from_080(hub.crm.get("C001").phone, at="2026-10-01T09:00")
    recall.plan(recall.today)
    assert ("김지수", "광고 수신동의 2년 확인") not in items_by_name(recall)


def test_draft_finishing_after_opt_out_does_not_revive_the_ad(hub, recall):
    recall.plan(recall.today)
    ad = items_by_name(recall)[("한도윤", "상담 후 미예약 재상담 안내")]
    recall.store.update(ad["id"], status="drafting")  # AI가 아직 쓰는 중

    hub.optout.from_080(hub.crm.get("C007").phone)
    recall.draft(ad["id"])  # 다 쓰고 나서 '승인 대기'로 덮어쓰면 안 된다
    assert recall.store.get(ad["id"])["status"] == "blocked"


NHN_UNSUBSCRIBE = {  # NHN Cloud SMS 웹훅 문서의 예시 모양
    "hooksId": "202610221010101010sadasdavas", "webhookConfigId": "cfg", "productName": "SMS", "appKey": "demo",
    "event": "UNSUBSCRIBE",
    "hooks": [{"hookId": "h1", "recipientNo": "", "unsubscribeNo": "0800000000", "enterpriseName": "데모성형외과",
               "createdDateTime": "2026-10-21T11:25:10.000+09:00"}],
}


def test_nhn_080_webhook(hub, recall, monkeypatch):
    client = TestClient(create_app(hub, recall))
    payload = {**NHN_UNSUBSCRIBE, "hooks": [{**NHN_UNSUBSCRIBE["hooks"][0], "recipientNo": "01024681357"}]}
    assert hub.crm.find_by_phone("01024681357").id == "C007"
    body = client.post("/webhooks/nhn-sms", json=payload).json()
    assert body["results"][0]["changed"] is True
    assert hub.crm.consent_history("C007")[-1].at == "2026-10-21T11:25"  # 한국 시간으로
    assert client.post("/webhooks/nhn-sms", json=payload).json()["results"][0]["changed"] is False  # 재전송
    assert client.post("/webhooks/nhn-sms", json={**payload, "event": "MESSAGE_RESULT_UPDATE"}).json() == {"results": []}

    monkeypatch.setenv("NHN_WEBHOOK_SIGNATURE", "s3cret")
    assert client.post("/webhooks/nhn-sms", json=payload).status_code == 401
    ok = client.post("/webhooks/nhn-sms", json=payload, headers={"X-Toast-Webhook-Signature": "s3cret"})
    assert ok.status_code == 200


def test_kakao_channel_webhook(hub, recall, monkeypatch):
    client = TestClient(create_app(hub, recall))
    blocked = {"event": "blocked", "id": "k_seoyeon", "id_type": "app_user_id",
               "channel_public_id": "_demo", "channel_uuid": "@데모성형외과", "updated_at": "2026-10-21T02:00:00Z"}
    assert client.post("/webhooks/kakao-channel", json=blocked).json()["results"][0]["changed"] is True
    customer = hub.crm.get("C004")
    assert customer.kakao_friend is False and hub.crm.consent_history("C004")[-1].at == "2026-10-21T11:00"
    assert client.post("/webhooks/kakao-channel", json={"event": "other"}).json() == {"results": []}

    monkeypatch.setenv("KAKAO_ADMIN_KEY", "admin-key")
    added = {**blocked, "event": "added"}
    assert client.post("/webhooks/kakao-channel", json=added, headers={"Authorization": "KakaoAK wrong"}).status_code == 401
    assert client.post("/webhooks/kakao-channel", json=added, headers={"Authorization": "KakaoAK admin-key"}).status_code == 200
    assert hub.crm.get("C004").kakao_friend is True

    key = next(t["key"] for t in client.get("/api/threads").json() if t["name"] == "박서연")
    assert client.get(f"/api/threads/{key}").json()["consent_history"][-1]["source"] == "카카오 채널 추가"


def test_recall_item_view_shows_consent_history(hub, recall):
    hub.optout.from_080(hub.crm.get("C007").phone)
    recall.plan(recall.today)
    view = recall.item_view(items_by_name(recall)[("한도윤", "상담 후 미예약 재상담 안내")]["id"])
    assert view["status"] == "blocked" and view["consent_history"][-1]["source"] == "080 수신거부"


# ---- 처리 결과 안내 문단 자리, AI가 수신거부를 또 말할 때 ----------------------------

@pytest.mark.parametrize("reply, expected", [
    ("안녕하세요, 오세린님. 보톡스 주기는 원장님 상담 후 안내드려요.", "안녕하세요, 오세린님.\n\n[안내]\n\n보톡스 주기는 원장님 상담 후 안내드려요."),
    ("오세린님, 안녕하세요. 데모성형외과 상담팀입니다.\n\n본문\n\n드림", "오세린님, 안녕하세요. 데모성형외과 상담팀입니다.\n\n[안내]\n\n본문\n\n드림"),
    ("보톡스 주기는 3~4개월이에요.", "[안내]\n\n보톡스 주기는 3~4개월이에요."),
])
def test_notice_goes_after_the_greeting(reply, expected):
    from hub.pipeline import _after_greeting

    assert _after_greeting(reply, "[안내]") == expected


def test_ai_repeating_the_opt_out_is_flagged(tmp_path):
    from hub.app import build_hub
    from hub.drafting.local import LocalDrafter, OllamaClient
    from tests.conftest import TODAY
    from tests.test_local import FakeOllama

    reply = "안녕하세요, [고객1]님. 앞으로 광고 메시지는 보내지 않도록 처리해드릴게요. 보톡스 주기는 상담 후 안내드려요."
    fake = FakeOllama(content={"summary": "요약", "draft_reply": reply, "draft_reply_ko": reply, "used_sources": []})
    try:
        hub = build_hub(runtime=tmp_path, crm_kind="sqlite", llm="local", today=TODAY)
        hub.drafter = LocalDrafter(OllamaClient(base_url=fake.url))
        hub.clock = lambda: datetime.combine(TODAY, time(12, 0))
        hub.reset()
        hub.sync()
        hub.sync()
        key = next(t["key"] for t in hub.store.threads() if t["customer_id"] == "C009")
        view = hub.thread_view(key)
        phrases = [f["phrase"] for f in view["flags"]]
        assert "수신거부 요청" in phrases and "수신거부 이야기 중복" in phrases
        assert view["analysis"]["draft_reply"].startswith("안녕하세요, 오세린님.\n\n요청하신 대로")
    finally:
        fake.close()
