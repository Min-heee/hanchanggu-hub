from hub.drafting import DrafterError


def test_first_sync_loads_batch_one_and_leaves_batch_two(hub):
    keys = {t["key"] for t in hub.thread_list()}
    assert len(keys) == 6
    assert hub.status()["remaining"] == 5
    assert all(t["status"] == "drafted" for t in hub.thread_list())


def test_same_customer_on_two_channels_becomes_one_thread(hub):
    hub.sync()
    jisu = [t for t in hub.thread_list() if t["name"] == "김지수"]
    assert len(jisu) == 1
    assert jisu[0]["channels"] == ["instagram", "kakao"]
    assert jisu[0]["message_count"] == 2


def test_each_question_in_a_merged_thread_gets_its_own_sources(hub):
    # 실제 Claude 실행에서 '부목' 질문의 자료가 '토요일 상담' 질문에 밀려 빠졌던 경우
    hub.sync()
    request, _ = hub.build_request("cust:C001")
    sections = {hub.knowledge.section_of(c) for c in request.knowledge}
    assert "회복 경과" in sections or "일상 복귀" in sections
    assert "상담 예약 방법" in sections or "진료 시간과 위치" in sections


def test_urgent_post_op_symptom_is_flagged_and_sorted_first(hub):
    first = hub.thread_list()[0]
    assert first["name"] == "박서연"
    assert first["urgency"] == "high"
    assert first["needs_human"] is True
    draft = hub.thread_view(first["key"])["analysis"]["draft_reply"]
    assert "응급실" in draft and "02-000-0000" in draft


def test_draft_uses_crm_history(hub):
    view = hub.thread_view("cust:C002")
    draft = view["analysis"]["draft_reply"]
    assert "이민호님" in draft
    assert "7일째" in draft
    assert "내일 실밥 제거" in draft


def test_prompt_sent_to_ai_contains_no_personal_data(hub):
    view = hub.thread_view("web:web_guest_5531")
    assert "정다은" not in view["prompt"]
    assert "010-7777-8888" not in view["prompt"]
    assert "[고객1]" in view["prompt"] and "[전화1]" in view["prompt"]
    # 사람이 보는 초안에는 실명이 다시 들어간다
    assert view["analysis"]["draft_reply"].startswith("정다은님")


def test_price_question_gets_policy_answer_without_amounts(hub):
    view = hub.thread_view("cust:C005")
    assert view["analysis"]["intent"] == "가격 문의"
    assert view["flags"] == []
    assert "서면" in view["analysis"]["draft_reply"]


def test_foreign_inquiry_gets_reply_in_customer_language_with_korean_copy(hub):
    hub.sync()
    view = hub.thread_view("instagram:ig_mika_jp")
    analysis = view["analysis"]
    assert analysis["language"] == "ja"
    assert "通訳" in analysis["draft_reply"]
    assert "통역" in analysis["draft_reply_ko"]


def test_approving_new_lead_registers_customer_and_logs_contact(hub):
    key = "web:web_guest_5531"
    result = hub.approve(key, "정다은님, 안녕하세요. 재수술 상담 가능한 날짜를 안내드릴게요.")

    customer = hub.crm.get(result["customer_id"])
    assert customer.name == "정다은"
    assert customer.channel_ids == {"web": "web_guest_5531"}
    assert hub.crm.find_by_phone("01077778888").id == customer.id
    logs = hub.crm.contact_logs(customer.id)
    assert len(logs) == 1 and "재수술" in logs[0].reply

    view = hub.thread_view(key)
    assert view["status"] == "sent"
    assert view["messages"][-1]["direction"] == "out"


def test_new_message_after_reply_reopens_thread(hub):
    hub.approve("cust:C001", "답장")
    assert hub.thread_view("cust:C001")["status"] == "sent"
    hub.sync()  # batch 2에 김지수 카카오 문의가 있다
    view = hub.thread_view("cust:C001")
    assert view["status"] == "drafted"
    assert "토요일" in view["prompt"] and "부목" not in view["prompt"].split("[문의 내용]")[1].split("[병원 안내 자료]")[0]


def test_escalation_is_logged_to_crm(hub):
    hub.escalate("cust:C004", "간호팀 전화 확인")
    assert hub.thread_view("cust:C004")["status"] == "escalated"
    assert hub.crm.contact_logs("C004")[-1].summary.startswith("[담당자 확인 요청]")


def test_claude_failure_falls_back_to_rule_draft(hub):
    class BrokenDrafter:
        mode = "claude"

        def draft(self, request):
            raise DrafterError("Claude API에 연결하지 못했어요.")

    hub.drafter = BrokenDrafter()
    hub.analyze("cust:C002")
    view = hub.thread_view("cust:C002")
    assert view["status"] == "drafted"
    assert view["mode"] == "offline"
    assert "연결하지 못했어요" in view["note"]


def test_claude_drafter_receives_masked_prompt_and_tokens_are_restored(hub):
    seen = {}

    class EchoDrafter:
        mode = "claude"

        def draft(self, request):
            seen["prompt"] = request.render_prompt()
            from hub.drafting.rules import RuleDrafter
            return RuleDrafter().draft(request)

    hub.drafter = EchoDrafter()
    hub.analyze("cust:C001")
    assert "김지수" not in seen["prompt"]
    assert hub.thread_view("cust:C001")["analysis"]["draft_reply"].startswith("김지수님")


def test_low_credit_error_tells_staff_what_to_do():
    import anthropic
    import httpx2
    import pytest

    from hub.drafting.claude import call_structured

    class NoCreditClient:
        class beta:
            class messages:
                @staticmethod
                def create(**kwargs):
                    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
                    response = httpx2.Response(400, request=request)
                    raise anthropic.BadRequestError(
                        "Your credit balance is too low to access the Anthropic API.", response=response, body=None
                    )

    with pytest.raises(DrafterError, match="크레딧"):
        call_structured(NoCreditClient(), "claude-opus-5-5", "system", "prompt", {})
