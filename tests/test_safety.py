from hub import compliance
from hub.privacy import Masker


def test_masker_hides_names_phones_emails_and_restores_them():
    masker = Masker(["김지수"])
    text = "저 김지수예요. 010-2345-6789나 jisu.kim@example.com으로 연락 주세요."
    masked = masker.mask(text)

    for secret in ("김지수", "010-2345-6789", "jisu.kim@example.com"):
        assert secret not in masked
    assert masker.unmask(masked) == text


def test_one_person_gets_one_token_however_the_name_is_written():
    # 다른 토큰을 받으면 AI가 '동일인 확인 필요'로 오해한다 (실제 Claude 실행에서 발견)
    masker = Masker(["Emily Chen"])
    assert masker.mask("Hi, this is Emily.") == "Hi, this is [고객1]."
    assert masker.mask("Emily Chen") == "[고객1]"

    korean = Masker(["박서연"], aliases={"서연": "박서연"})
    assert korean.mask("박서연입니다. 서연이에요.") == "[고객1]입니다. [고객1]이에요."
    assert korean.unmask("[고객1]님") == "박서연님"


def test_masker_leaves_dates_and_clinic_numbers_alone():
    masker = Masker([])
    text = "2026-10-21 09:05에 02-000-0000으로 전화했어요."
    assert masker.mask(text) == text


def test_compliance_flags_guarantees_prices_and_superlatives():
    phrases = {f["phrase"] for f in compliance.check("부작용 없이 100% 만족! 업계 최고, 지금 30만원 할인 중")}
    assert {"100%", "최고", "할인"} <= phrases
    assert any("부작용" in p for p in phrases)
    assert any("만원" in p for p in phrases)


def test_compliance_ignores_safe_wording():
    safe = "회복 속도는 사람마다 차이가 있어요. 비용은 원장 상담 후 서면으로 안내드려요. 10시 원장님 상담 가능해요."
    assert compliance.check(safe) == []
