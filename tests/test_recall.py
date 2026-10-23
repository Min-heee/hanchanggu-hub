import dataclasses
import re
from datetime import date, datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient

from hub import compliance
from hub.app import build_recall, create_app
from hub.drafting import DrafterError
from hub.models import Appointment, ConsentEvent, Customer
from hub.recall import delivery
from hub.recall.delivery import CHANNELS
from hub.recall.rules import RecallRule


@pytest.fixture
def recall(hub):
    r = build_recall(hub)
    # 실제 시계에 기대면 테스트를 언제 돌리느냐에 따라 결과가 달라진다. 오늘 11시(10시 발송 시각 이후)로 고정한다.
    r.clock = lambda: at(r, 0, 11, 0)
    return r


def at(recall, day_offset, hour, minute=0):
    return datetime.combine(recall.today + timedelta(days=day_offset), time(hour, minute))


def iso(recall, day_offset, hm):
    return f"{recall.today + timedelta(days=day_offset)}T{hm}"


def by_customer(items):
    return {(i["customer_name"], i["rule_name"]): i for i in items}


def today_items(recall):
    return by_customer(recall.items(recall.today.isoformat()))


def change_customers(recall, consents=None, **changes):
    """CRM 연결부의 reset으로 고객 정보를 바꾼다 (SQLite·CSV 둘 다에서 돈다). changes: {고객ID: {필드: 값}}
    동의 이력은 그대로 두고, consents로 고객별 이력을 바꿀 수 있다."""
    customers = recall.crm.all_customers()
    history = {c.id: recall.crm.consent_history(c.id) for c in customers} | (consents or {})
    recall.crm.reset([dataclasses.replace(c, **changes.get(c.id, {})) for c in customers], history)


def outbox(recall):
    path = recall.channels.outbox_path
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


# ---- 대상 찾기 ---------------------------------------------------------------

def test_today_finds_each_rule_and_explains_who_is_held_back(recall):
    result = recall.plan(recall.today)
    items = today_items(recall)

    ready = {k for k, i in items.items() if i["status"] == "ready"}
    assert ready == {
        ("윤채원", "수술 다음날 안부"),
        ("이민호", "예약 전날 안내"),
        ("장민서", "1개월 경과 확인 예약 안내"),
        ("한도윤", "상담 후 미예약 재상담 안내"),
        ("오세린", "쁘띠 시술 3개월 안부"),
        ("김지수", "광고 수신동의 2년 확인"),  # 2024.10.23 홈페이지 회원가입 때 동의 → 10월 22일까지 알려야 한다
    }
    assert result == {"date": recall.today.isoformat(), "created": 6, "blocked": 3}
    assert "외국어" in items[("Li Na", "수술 다음날 안부")]["block_reason"]
    assert "수신 동의" in items[("서하린", "상담 후 미예약 재상담 안내")]["block_reason"]
    assert "답하지 않은 문의" in items[("김지수", "상담 후 미예약 재상담 안내")]["block_reason"]


def test_planning_twice_or_on_a_later_day_does_not_resend(recall):
    recall.plan(recall.today)
    recall.plan(recall.today)
    assert len([i for i in recall.items(recall.today.isoformat()) if i["status"] != "blocked"]) == 6

    item = today_items(recall)[("한도윤", "상담 후 미예약 재상담 안내")]
    assert recall.approve(item["id"])["scheduled_at"] == iso(recall, 0, "11:00")
    tomorrow = recall.today + timedelta(days=1)
    recall.plan(tomorrow)
    assert ("한도윤", "상담 후 미예약 재상담 안내") not in by_customer(recall.items(tomorrow.isoformat()))


def test_reminders_still_go_to_a_customer_with_an_urgent_inquiry(recall):
    # 박서연은 문의함에 긴급 문의가 열려 있지만, 다음 예약 전날 안내는 보내야 한다
    visit = next(a for a in recall.crm.get("C004").appointments if a.status == "예정")
    day = date.fromisoformat(visit.date) - timedelta(days=1)
    recall.plan(day)
    item = by_customer(recall.items(day.isoformat()))[("박서연", "예약 전날 안내")]
    assert item["status"] == "ready"


def test_answering_the_inquiry_releases_the_held_message(hub, recall):
    # 김지수가 20일 전에 다시 동의한 것으로 두어 2년 확인·처리 결과 안내(14일)가 같은 날 나가지 않게 한다 ('하루 1건')
    recent = ConsentEvent(at=f"{recall.today - timedelta(days=20)}T09:00", field="marketing_consent", value=True, source="상담 동의서")
    change_customers(recall, consents={"C001": [recent]})
    recall.plan(recall.today)
    hub.approve("cust:C001", "답장")
    recall.plan(recall.today)
    assert today_items(recall)[("김지수", "상담 후 미예약 재상담 안내")]["status"] == "ready"


def test_customer_without_phone_is_held_back_in_the_queue(recall):
    change_customers(recall, C006={"phone": ""}, C007={"phone": ""})
    assert recall.plan(recall.today) == {"date": recall.today.isoformat(), "created": 4, "blocked": 5}  # 김지수 2년 확인 포함
    items = today_items(recall)
    # 정보성 템플릿(알림톡) 하나와 AI 광고(문자) 하나: 둘 다 채우거나 쓰지 않고 보류해야 한다
    for key in [("윤채원", "수술 다음날 안부"), ("한도윤", "상담 후 미예약 재상담 안내")]:
        item = items[key]
        assert item["status"] == "blocked" and item["channel"] == "" and "휴대폰 번호" in item["block_reason"]
        assert item["channel_label"] == "보낼 경로 없음" and item["recipient_display"] == ""
        assert not item.get("text")
        assert recall.item_view(item["id"])["channel_label"] == "보낼 경로 없음"


# ---- 발송 경로 (정보성 → 알림톡, 광고성 → 브랜드 메시지/문자) ----------------------

def test_info_goes_by_alimtalk_and_ads_by_brand_message_or_sms(recall):
    recall.plan(recall.today)
    items = today_items(recall)
    for key in [("윤채원", "수술 다음날 안부"), ("이민호", "예약 전날 안내"), ("장민서", "1개월 경과 확인 예약 안내")]:
        assert items[key]["channel"] == "alimtalk"
    # 오세린은 카카오 채널 친구 → 브랜드 메시지(구 친구톡), 한도윤은 친구가 아님 → 문자
    assert items[("오세린", "쁘띠 시술 3개월 안부")]["channel"] == "friendtalk"
    assert items[("한도윤", "상담 후 미예약 재상담 안내")]["channel"] == "sms"
    # 보류된 건도 경로는 정해 둔다. 해외 번호는 보낼 경로가 없다
    assert items[("Li Na", "수술 다음날 안부")]["channel"] == ""
    assert items[("김지수", "상담 후 미예약 재상담 안내")]["channel"] == "friendtalk"
    assert items[("서하린", "상담 후 미예약 재상담 안내")]["channel"] == "sms"
    # 고객이 먼저 말을 건 대화용 채널은 리콜에 쓰지 않는다
    assert not {i["channel"] for i in items.values()} & {"instagram", "kakao", "naver", "web", "email"}


def test_route_rules():
    ad = RecallRule(id="a", name="a", kind="광고성", delivery="ai", trigger="after_procedure", timing="", min_days=0, max_days=0)
    info_template = dataclasses.replace(ad, kind="정보성", delivery="template")
    info_ai = dataclasses.replace(ad, kind="정보성", delivery="ai")
    domestic = Customer(id="X", name="X", phone="010-1234-5678", marketing_consent=True)

    assert delivery.route(info_template, domestic)[0] == "alimtalk"
    assert delivery.route(info_ai, domestic)[0] == "sms"  # 개인화한 문구는 알림톡 템플릿에 실을 수 없다
    assert delivery.route(ad, domestic)[0] == "sms"
    assert delivery.route(ad, dataclasses.replace(domestic, kakao_friend=True))[0] == "friendtalk"
    assert delivery.route(ad, dataclasses.replace(domestic, phone="+82 10-1234-5678"))[0] == "sms"

    emily = Customer(id="C003", name="Emily Chen", email="emily.chen@example.com", marketing_consent=True)
    channel, _, problem = delivery.route(ad, emily)
    assert channel is None and "휴대폰 번호" in problem and "코디네이터" in problem

    for rule in (ad, info_template):
        lina = Customer(id="C011", name="Li Na", phone="+86 138 0013 8000", marketing_consent=True, language="ko")
        channel, _, problem = delivery.route(rule, lina)
        assert channel is None and "해외 번호" in problem


# ---- 문구와 광고 표시 ---------------------------------------------------------

def test_alimtalk_templates_are_filled_and_never_edited(recall):
    recall.plan(recall.today)
    item = today_items(recall)[("윤채원", "수술 다음날 안부")]
    assert "윤채원님" in item["text"] and "다음 내원은" in item["text"] and "{" not in item["text"]
    recall.approve(item["id"], text="마음대로 바꾼 문구")
    assert recall.store.get(item["id"])["text"] == item["text"]


def test_templates_do_not_ask_for_replies_that_never_reach_the_inbox(recall):
    recall.plan(recall.today)
    for item in today_items(recall).values():
        if item.get("text"):
            assert not re.search(r"답장(?:으로| 주| 부탁)", item["text"])  # '답장을 기다리지 마시고'는 괜찮다
            assert "오늘 중으로" not in item["text"]


def test_ad_messages_put_sender_and_contact_first_and_opt_out_last(recall):
    recall.plan(recall.today)
    items = today_items(recall)
    for key in [("한도윤", "상담 후 미예약 재상담 안내"), ("오세린", "쁘띠 시술 3개월 안부")]:
        item = items[key]
        assert item["text"].startswith("(광고) 데모성형외과 대표번호 02-000-0000")
        assert item["text"].rstrip().endswith(CHANNELS[item["channel"]].opt_out)
        assert item["flags"] == []


def test_ad_opt_out_text_matches_the_channel(recall):
    recall.plan(recall.today)
    items = today_items(recall)
    sms = items[("한도윤", "상담 후 미예약 재상담 안내")]
    brand = items[("오세린", "쁘띠 시술 3개월 안부")]
    assert sms["text"].endswith("무료수신거부 080-000-0000") and "채널 차단" not in sms["text"]
    assert "채널 차단" in brand["text"] and "무료" in brand["text"] and CHANNELS["sms"].opt_out not in brand["text"]
    # 브랜드 메시지 문구를 문자로 보내려 하면 막는다 (화면 실시간 점검도 같은 규칙)
    wrong = sms["text"].replace(CHANNELS["sms"].opt_out, CHANNELS["friendtalk"].opt_out)
    assert any(f["phrase"] == "수신거부 방법 없음" and f["blocking"] for f in recall.check(sms["id"], wrong))
    with pytest.raises(ValueError, match="수신거부"):
        recall.approve(sms["id"], text=wrong)


def test_ad_message_without_labels_cannot_be_sent(recall):
    recall.plan(recall.today)
    item = today_items(recall)[("오세린", "쁘띠 시술 3개월 안부")]
    with pytest.raises(ValueError, match="광고"):
        recall.approve(item["id"], text="오세린님, 잘 지내시죠? 상담 일정 잡아드릴게요.")
    with pytest.raises(ValueError, match="수신거부"):  # 수신거부 뒤에 광고 문구를 덧붙이면 안 된다
        recall.approve(item["id"], text=item["text"] + "\n지금 예약하시면 상담 일정을 먼저 잡아드려요.")
    assert recall.store.get(item["id"])["status"] == "ready"


def test_ad_format_check_placement():
    assert compliance.check_ad_format("(광고) 데모성형외과\n안녕하세요\n수신거부: 답장", "데모성형외과") == []
    phrases = {f["phrase"] for f in compliance.check_ad_format("안녕하세요", "데모성형외과")}
    assert phrases == {"(광고) 표기 없음", "보낸 곳 없음", "수신거부 방법 없음"}

    opt_out, contact = "무료수신거부 080-000-0000", "02-000-0000"
    good = f"(광고) 데모성형외과 대표번호 {contact}\n\n안녕하세요.\n\n{opt_out}"
    assert compliance.check_ad_format(good, "데모성형외과", opt_out, contact, "(광고) 데모성형외과 안내") == []
    sender_late = f"(광고) 오늘 시술 안내 드려요. 데모성형외과 {contact}\n\n{opt_out}"
    assert {f["phrase"] for f in compliance.check_ad_format(sender_late, "데모성형외과", opt_out, contact)} == {"보낸 곳 없음"}
    no_contact = f"(광고) 데모성형외과\n\n안녕하세요.\n\n{opt_out}"
    assert {f["phrase"] for f in compliance.check_ad_format(no_contact, "데모성형외과", opt_out, contact)} == {"연락처 없음"}
    opt_out_mid = f"(광고) 데모성형외과 {contact}\n{opt_out} 그리고 추가 광고 문구가 계속됩니다."
    assert {f["phrase"] for f in compliance.check_ad_format(opt_out_mid, "데모성형외과", opt_out, contact)} == {"수신거부 방법 없음"}
    bad_subject = compliance.check_ad_format(good, "데모성형외과", opt_out, contact, "데모성형외과 안내")
    assert [f["phrase"] for f in bad_subject] == ["제목 (광고) 표기 없음"]


def test_messages_too_long_for_the_channel_are_blocked(recall):
    recall.plan(recall.today)
    items = today_items(recall)
    brand = items[("오세린", "쁘띠 시술 3개월 안부")]
    with pytest.raises(ValueError, match=r"\d+자"):
        recall.approve(brand["id"], text=brand["text"].replace("잘 지내고 계신가요?", "잘 지내고 계신가요?" + "가" * 1000))
    sms = items[("한도윤", "상담 후 미예약 재상담 안내")]
    with pytest.raises(ValueError, match=r"\d+바이트"):
        recall.approve(sms["id"], text=sms["text"].replace("\n\n무료수신거부", "가" * 1000 + "\n\n무료수신거부"))


def test_length_limits():
    assert delivery.length_flags("sms", "가" * 1000) == []
    assert delivery.length_flags("sms", "가" * 1001)[0]["phrase"] == "2002바이트"
    assert delivery.length_flags("sms", "a" * 2000) == []
    assert delivery.length_flags("sms", "a" * 2001)[0]["phrase"] == "2001바이트"
    assert delivery.length_flags("friendtalk", "가" * 1000) == []
    assert delivery.length_flags("alimtalk", "가" * 1001)[0]["phrase"] == "1001자"


# ---- 발송 시각과 예약 ---------------------------------------------------------

def test_send_time_rules():
    day = datetime(2026, 10, 21)
    for channel in ("friendtalk", "sms"):
        start, end = CHANNELS[channel].ad_hours
        just_before_end = datetime.combine(day, end) - timedelta(minutes=1)
        assert delivery.send_time(channel, True, just_before_end) == just_before_end
        assert delivery.send_time(channel, True, datetime.combine(day, end)) == datetime(2026, 10, 22, 10, 0)
        assert delivery.send_time(channel, True, datetime.combine(day, start)) == datetime.combine(day, start)
        early = datetime.combine(day, start) - timedelta(minutes=1)
        assert delivery.send_time(channel, True, early) == datetime(2026, 10, 21, 10, 0)
        assert delivery.send_time(channel, False, day.replace(hour=23)) == day.replace(hour=23)
    # 수신 시각 기준이라 문자도 20:50에 끊는다
    assert delivery.send_time("sms", True, day.replace(hour=20, minute=55)) == datetime(2026, 10, 22, 10, 0)
    assert delivery.send_time("alimtalk", False, day.replace(hour=23)) == day.replace(hour=23)


def test_daytime_approval_before_send_time_becomes_a_reservation(recall):
    recall.clock = lambda: at(recall, 0, 8, 30)
    recall.plan(recall.today)
    item = today_items(recall)[("오세린", "쁘띠 시술 3개월 안부")]
    assert item["send_now"] is False and item["send_label"] == "10:00"
    approved = recall.approve(item["id"])
    assert approved["status"] == "scheduled" and approved["scheduled_at"] == iso(recall, 0, "10:00")
    assert outbox(recall) == [] and recall.crm.contact_logs("C009") == []

    recall.clock = lambda: at(recall, 0, 10, 1)
    assert recall.dispatch_due() == 1
    sent = recall.store.get(item["id"])
    assert sent["status"] == "sent" and sent["sent_at"] == iso(recall, 0, "10:00")
    assert recall.crm.contact_logs("C009")[-1].at == iso(recall, 0, "10:00")


def test_ads_approved_at_night_go_out_next_morning_and_info_goes_now(recall):
    recall.plan(recall.today)
    items = today_items(recall)
    recall.clock = lambda: at(recall, 0, 22, 30)

    ad = recall.approve(items[("한도윤", "상담 후 미예약 재상담 안내")]["id"])
    assert ad["status"] == "scheduled" and ad["scheduled_at"] == iso(recall, 1, "10:00")
    tomorrow = recall.today + timedelta(days=1)
    assert recall._decorate(ad)["send_label"] == f"발송 예약 · {tomorrow.month}/{tomorrow.day} 10:00"
    assert not [line for line in outbox(recall) if "080-000-0000" in line]  # 밤에는 아직 아무것도 나가지 않았다
    assert recall.crm.contact_logs("C007") == []

    info = recall.approve(items[("이민호", "예약 전날 안내")]["id"])  # 진료 안내는 밤이어도 바로 나간다
    assert info["status"] == "sent" and info["sent_at"] == iso(recall, 0, "22:30")
    assert recall.crm.contact_logs("C002")[-1].at == iso(recall, 0, "22:30")

    recall.clock = lambda: at(recall, 1, 10, 5)
    assert recall.dispatch_due() == 1
    sent = recall.store.get(ad["id"])
    assert sent["status"] == "sent" and sent["sent_at"] == sent["scheduled_at"] == iso(recall, 1, "10:00")
    log = recall.crm.contact_logs("C007")[-1]
    assert log.at == iso(recall, 1, "10:00") and log.summary.startswith("[리콜]")
    record = [line for line in outbox(recall) if "080-000-0000" in line][-1]
    assert f'"send_at": "{iso(recall, 1, "10:00")}"' in record and '"ad": true' in record
    assert '"subject": "(광고) 데모성형외과 안내"' in record


def test_reservation_is_rechecked_right_before_sending(recall):
    recall.plan(recall.today)
    items = today_items(recall)
    recall.clock = lambda: at(recall, 0, 22, 30)
    sms_ad = recall.approve(items[("한도윤", "상담 후 미예약 재상담 안내")]["id"])
    brand_ad = recall.approve(items[("오세린", "쁘띠 시술 3개월 안부")]["id"])

    # 밤사이 한도윤은 080으로 수신 동의를 철회했고, 오세린은 카카오 채널을 차단했다
    change_customers(recall, C007={"marketing_consent": False}, C009={"kakao_friend": False})
    recall.clock = lambda: at(recall, 1, 10, 5)
    assert recall.dispatch_due() == 0
    held_sms, held_brand = recall.store.get(sms_ad["id"]), recall.store.get(brand_ad["id"])
    assert held_sms["status"] == "blocked" and held_sms["block_reason"].startswith("발송 직전 재확인") and "수신 동의" in held_sms["block_reason"]
    assert held_brand["status"] == "blocked" and "경로가 바뀌었어요" in held_brand["block_reason"]
    assert outbox(recall) == []


def test_reservation_can_be_cancelled_but_sent_messages_cannot_be_skipped(recall):
    recall.plan(recall.today)
    items = today_items(recall)
    with pytest.raises(ValueError, match="예약된 메시지만"):
        recall.cancel(items[("한도윤", "상담 후 미예약 재상담 안내")]["id"])

    recall.clock = lambda: at(recall, 0, 22, 30)
    ad = recall.approve(items[("한도윤", "상담 후 미예약 재상담 안내")]["id"])
    recall.cancel(ad["id"])
    assert recall.store.get(ad["id"])["status"] == "skipped" and recall.store.get(ad["id"])["note"] == "예약 취소"
    recall.clock = lambda: at(recall, 1, 10, 5)
    assert recall.dispatch_due() == 0 and outbox(recall) == []

    info = recall.approve(items[("이민호", "예약 전날 안내")]["id"])
    with pytest.raises(ValueError, match="건너뛸 수 없어요"):
        recall.skip(info["id"], "")


def test_one_ad_per_day_counts_the_day_it_actually_goes_out(recall):
    # 한도윤이 내일 시술 90일째가 되게 해서, 오늘 재상담 안내와 내일 쁘띠 안부가 둘 다 걸리게 한다
    hdy = recall.crm.get("C007")
    botox = Appointment(date=(recall.today - timedelta(days=89)).isoformat(), kind="시술", procedure="보톡스(사각턱)", status="완료")
    change_customers(recall, C007={"appointments": hdy.appointments + [botox]})
    tomorrow = recall.today + timedelta(days=1)

    # 순서 1: 오늘 광고를 밤에 승인해 내일 10시로 미뤄졌다 → 내일 계획에서 쁘띠 안부는 보류
    recall.plan(recall.today)
    recall.clock = lambda: at(recall, 0, 22, 30)
    recall.approve(today_items(recall)[("한도윤", "상담 후 미예약 재상담 안내")]["id"])
    recall.plan(tomorrow)
    petit = by_customer(recall.items(tomorrow.isoformat()))[("한도윤", "쁘띠 시술 3개월 안부")]
    assert petit["status"] == "blocked" and "하루 1건" in petit["block_reason"]


def test_one_ad_per_day_is_also_checked_at_approval(recall):
    hdy = recall.crm.get("C007")
    botox = Appointment(date=(recall.today - timedelta(days=89)).isoformat(), kind="시술", procedure="보톡스(사각턱)", status="완료")
    change_customers(recall, C007={"appointments": hdy.appointments + [botox]})
    tomorrow = recall.today + timedelta(days=1)

    # 순서 2: 내일 쁘띠 안부를 먼저 보내고, 오늘 재상담 안내를 밤에 승인하면 내일로 밀려 같은 날 2건이 된다 → 막는다
    recall.plan(recall.today)
    recall.plan(tomorrow)
    recall.clock = lambda: at(recall, 1, 11, 0)
    recall.approve(by_customer(recall.items(tomorrow.isoformat()))[("한도윤", "쁘띠 시술 3개월 안부")]["id"])
    recall.clock = lambda: at(recall, 0, 22, 30)
    with pytest.raises(ValueError, match="하루 1건"):
        recall.approve(today_items(recall)[("한도윤", "상담 후 미예약 재상담 안내")]["id"])


# ---- 승인·기록 ---------------------------------------------------------------

def test_sending_logs_to_crm(recall):
    recall.plan(recall.today)
    item = today_items(recall)[("이민호", "예약 전날 안내")]
    recall.approve(item["id"])
    log = recall.crm.contact_logs("C002")[-1]
    assert log.summary.startswith("[리콜] 예약 전날 안내") and log.channel.startswith("알림톡")
    assert log.at == iso(recall, 0, "11:00")


def test_bulk_approve_only_touches_info_templates(recall):
    recall.plan(recall.today)
    assert recall.approve_all_info(recall.today.isoformat()) == 4
    statuses = {(i["customer_name"], i["status"]) for i in recall.items(recall.today.isoformat()) if i["kind"] == "광고성"}
    assert ("한도윤", "ready") in statuses and ("오세린", "ready") in statuses


def test_ai_writer_gets_masked_prompt_and_falls_back_to_template(recall):
    seen = []

    class BrokenWriter:
        mode = "claude"

        def write(self, prompt):
            seen.append(prompt)
            raise DrafterError("Claude API에 연결하지 못했어요.")

    recall.writer = BrokenWriter()
    recall.plan(recall.today)
    item = today_items(recall)[("한도윤", "상담 후 미예약 재상담 안내")]
    assert seen and all("한도윤" not in p for p in seen)
    assert any("이직" in p for p in seen)  # 상담 메모는 개인화에 쓰도록 넘긴다 (두 건이 동시에 써져 순서는 정해지지 않음)
    assert item["mode"] == "template" and "기본 문구로 대신" in item["note"]
    assert item["text"].startswith("(광고)")


def test_live_check_endpoint_uses_the_same_rules_as_approval(hub, recall):
    recall.plan(recall.today)
    client = TestClient(create_app(hub, recall))
    sms = today_items(recall)[("한도윤", "상담 후 미예약 재상담 안내")]
    wrong = sms["text"].replace(CHANNELS["sms"].opt_out, CHANNELS["friendtalk"].opt_out)

    flags = client.post(f"/api/recall/items/{sms['id']}/check", json={"text": wrong}).json()["flags"]
    assert any(f["phrase"] == "수신거부 방법 없음" and f["blocking"] for f in flags)
    assert client.post(f"/api/recall/items/{sms['id']}/approve", json={"text": wrong}).status_code == 400
    too_long = sms["text"].replace("\n\n무료수신거부", "가" * 1000 + "\n\n무료수신거부")
    flags = client.post(f"/api/recall/items/{sms['id']}/check", json={"text": too_long}).json()["flags"]
    assert any(f["phrase"].endswith("바이트") and f["blocking"] for f in flags)
    assert client.post(f"/api/recall/items/{sms['id']}/approve", json={"text": sms["text"]}).status_code == 200


def test_kakao_friend_flag_survives_both_crm_adapters(recall):
    assert recall.crm.get("C009").kakao_friend is True
    assert recall.crm.get("C007").kakao_friend is False


def test_ad_framing_replaces_whatever_legal_lines_the_model_wrote(recall):
    body = "오세린님, 잘 지내시죠?\n대표번호나 카카오톡 채널 채팅으로 편하게 연락 주세요."
    expected = f"(광고) 데모성형외과 대표번호 02-000-0000\n\n{body}\n\n{CHANNELS['friendtalk'].opt_out}"
    assert recall.frame_ad(body, "friendtalk") == expected
    messy = f"(광고) 데모성형외과\n\n{body}\n\n수신거부: 카카오톡 채널 홈 > 채널 차단\n대표번호 02-000-0000\n"
    assert recall.frame_ad(messy, "friendtalk") == expected
    assert compliance.check_ad_format(expected, "데모성형외과", CHANNELS["friendtalk"].opt_out, "02-000-0000") == []
