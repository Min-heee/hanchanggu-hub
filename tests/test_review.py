"""로컬 AI(qwen3:8b) 실제 실행에서 나온 문장으로 자동 검토를 확인한다."""
from hub import review


def reasons(text):
    return [f["reason"] for f in review.check(text)]


def test_catches_date_commitments_from_real_runs():
    assert any("예약을 확정" in r for r in reasons("We can schedule your consultation on December 9th once you arrive in Seoul."))
    assert any("예약을 확정" in r for r in reasons("12월 방문 시 12월 9일 상담 일정을 조율해 드리고, 통역사가 함께합니다."))


def test_catches_medical_judgment_from_real_runs():
    assert any("의학적 판단" in r for r in reasons("회사 출근은 수술 후 1~2주 경과 확인 전까지는 피하시는 게 좋습니다."))


def test_leaves_careful_wording_alone():
    careful = (
        "12월 9일 대면 상담 가능 여부를 확인한 뒤 다시 안내드리겠습니다. "
        "다음 주 월요일 출근 가능 여부는 내일 실밥 제거로 내원하실 때 원장님과 확인해 주세요. "
        "We will check availability for a consultation on December 9 and get back to you."
    )
    assert review.check(careful) == []


def test_catches_customer_phone_used_as_contact_number():
    flags = review.customer_contact_in_reply("상담 가능 시간을 확인해드릴 수 있도록 [전화1]로 연락 주세요.")
    assert flags and "병원 대표번호" in flags[0]["reason"]
    assert review.customer_contact_in_reply("대표번호 02-000-0000으로 연락 주세요.") == []


def test_catches_internal_source_ids_from_real_runs():
    assert any("자료 번호" in r for r in reasons("수술 후 관리는 [aftercare-3]에 안내드린 내용을 참고해 주세요."))


def test_made_up_name_tokens_never_reach_the_customer():
    from hub.privacy import Masker

    # 이름 없는 고객인데 로컬 모델이 '[고객1]님'을 지어낸 실제 사례
    assert Masker([]).unmask("안녕하세요. [고객1]님, 불편을 드려 죄송합니다.") == "안녕하세요. 불편을 드려 죄송합니다."
    known = Masker(["김지수"])
    assert known.unmask(known.mask("김지수님, [고객2]님")) == "김지수님, "  # 아는 토큰만 되돌리고 모르는 토큰은 지운다


# 로컬 모델(qwen3:8b)이 실제로 쓴 광고 리콜 두 건
QWEN_SMS_AD = (
    "(광고) 데모성형외과\n한도윤님, 10월 6일 상담 때 CT 촬영 결과 확인하셨죠? 회복 기간에 대해 궁금증이 남으셨다면 언제든지 연락 주세요. "
    "원장님이 재상담을 도와드릴게요. 수술 일정은 내년 3월 전까지 고려해 주세요.\n\n무료수신거부 080-000-0000\n대표번호 02-000-0000"
)
QWEN_BRAND_AD = (
    "(광고) 데모성형외과\n오세린님, 쁘띠 시술 후 3개월이 지났습니다. 경과 확인을 위해 연락드려요. 사진을 보내주시면 상담실장이 확인해드릴게요. "
    "재시술이 필요하시면 언제든지 연락주세요. 수신거부: 카카오톡 채널 차단\n대표번호 02-000-0000"
)


def test_recall_review_catches_what_the_local_model_actually_wrote():
    sms = " ".join(f["reason"] for f in review.check_recall(QWEN_SMS_AD))
    assert "재촉" in sms and "검사" in sms
    brand = " ".join(f["reason"] for f in review.check_recall(QWEN_BRAND_AD))
    assert "재시술" in brand and "사진" in brand


def test_recall_review_leaves_the_clinic_templates_alone():
    import json
    from pathlib import Path

    rules = json.loads((Path(__file__).parent.parent / "data" / "recall_rules.json").read_text(encoding="utf-8"))["rules"]
    for rule in rules:
        assert review.check_recall(rule["template"]) == [], rule["id"]
    # 문의 답장에서는 사진·검사 언급이 정상이라 일반 검토(check)는 건드리지 않는다
    assert review.check("상담 때 CT 촬영이 가능하니 시간을 넉넉히 잡아 주세요.") == []


def test_flags_phone_numbers_the_clinic_never_gave():
    # 로컬 모델이 실제로 대표번호를 지어냈다
    live = "상담실로 연락 주세요. (02-1234-5678) / 카카오톡 채널 '데모성형외과'도 이용 가능합니다."
    allowed = {"02-000-0000", "무료수신거부 080-000-0000"}
    assert [f["phrase"] for f in review.unknown_numbers(live, allowed)] == ["02-1234-5678"]
    assert review.unknown_numbers("대표번호 02-000-0000 · 무료수신거부 080-000-0000", allowed) == []
    assert review.unknown_numbers("Please call +82-2-000-0000.", {"02-000-0000", "+82-2-000-0000"}) == []
    assert review.unknown_numbers("10월 22일 10:00, 2~3일, 1,300자", allowed) == []


def test_recall_review_catches_timing_tied_to_the_customers_plans():
    live = "회복 기간을 고려해 내년 3월 이직 전 수술을 계획하셨다면, 원장 상담을 통해 궁금증을 확인해드릴 수 있습니다."
    assert any("일정에 맞춰" in f["reason"] for f in review.check_recall(live))
