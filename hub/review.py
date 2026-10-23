"""AI 초안 자동 검토: 실제 실행에서 AI가 실제로 저지른 실수 유형을 찾아 상담실장에게 경고한다.

'자료에 근거 없는 문장'을 글자 겹침으로 찾는 방법도 시험했지만, 지어낸 문장과 정상 문장의 점수가
섞여서 쓸 수 없었다. 그래서 확인된 실수 유형만 정확히 잡는다. 경고일 뿐 발송을 막지는 않는다.
"""
from __future__ import annotations

import re

_DATE = (
    r"\d{1,2}\s*월\s*\d{1,2}\s*일|\d{1,2}/\d{1,2}|\d{1,2}月\d{1,2}日"
    r"|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}"
)
_COMMIT = (
    r"가능합니다|가능해요|조율해\s?드리|잡아\s?드리|예약해\s?드리|확정"
    r"|can (?:schedule|arrange|book)|will (?:schedule|book)|(?:is|are) available|可能です"
)
_JUDGMENT = re.compile(
    r"(?:출근|복귀|근무|운동|사우나|음주|화장|렌즈)[^.\n。]{0,30}"
    r"(?:괜찮|가능합니다|가능해요|하셔도 됩니다|피하시는 게 좋|피하세요|삼가)"
)
_SENTENCES = re.compile(r"(?<=[.!?。])\s+|\n+")
_SOURCE_ID = re.compile(r"\[?\b(?:policy|nose|eye|contour_petit|aftercare)-\d+\b\]?")


def _flag(sentence: str, reason: str) -> dict:
    snippet = sentence if len(sentence) <= 40 else sentence[:40] + "…"
    return {"phrase": snippet, "reason": reason, "blocking": False, "kind": "review"}


def check(text: str) -> list[dict]:
    flags = []
    for sentence in (s.strip() for s in _SENTENCES.split(text or "")):
        if not sentence:
            continue
        if _SOURCE_ID.search(sentence):
            flags.append(_flag(sentence, "내부 안내 자료 번호가 답장에 들어 있어요. 고객이 알아볼 수 있는 말로 고쳐 주세요."))
        elif re.search(_DATE, sentence) and re.search(_COMMIT, sentence, re.IGNORECASE):
            flags.append(_flag(sentence, "특정 날짜 예약을 확정하는 표현이에요. 실제 일정을 확인했는지 보세요."))
        elif _JUDGMENT.search(sentence):
            flags.append(_flag(sentence, "의학적 판단으로 읽힐 수 있어요. 안내 자료와 같은 내용인지, 원장 확인이 필요한지 보세요."))
    return flags


_RECALL_RULES = [
    (
        re.compile(r"(?:수술|시술)[^.\n?]{0,20}(?:전까지|서두르|놓치|마감|늦기 전|고려해\s?(?:주세요|보세요))"),
        "수술·시술을 재촉하는 표현이에요. 원하면 편하게 연락 달라는 정도로만 안내해 주세요.",
    ),
    (
        re.compile(r"(?:이직|휴가|방학|결혼|입사|출국)[^.\n?]{0,10}(?:전|까지|맞춰|앞두고)[^.\n?]{0,10}(?:수술|시술)"),
        "고객 일정에 맞춰 수술·시술 시기를 짚는 표현이에요. 시기는 원장 상담에서 정해요.",
    ),
    (re.compile(r"재시술|리터치|추가\s?시술"), "재시술을 권하는 것으로 읽힐 수 있어요. 필요 여부는 원장 진료에서 정해요."),
    (
        re.compile(r"사진[^.\n?]{0,12}(?:보내|올려)"),
        "경과 사진은 내원 때 촬영하고 경과 판단은 원장 진료로 안내해 주세요 (병원 안내 자료와 다름).",
    ),
    (
        re.compile(r"(?<![A-Za-z])(?:CT|MRI)(?![A-Za-z])|엑스레이|검사\s?결과"),
        "광고 메시지에 검사 내용이 들어 있어요. 잠금화면에 보일 수 있고, CRM에 없는 내용(결과 설명 여부 등)인지 확인해 주세요.",
    ),
]


def check_recall(text: str) -> list[dict]:
    """AI가 쓴 리콜 메시지 전용 검토. 문의 답장에서는 사진·검사 언급이 정상이라 check()와 나눴다.

    로컬 모델이 실제로 쓴 광고 문구('수술 일정은 내년 3월 전까지 고려해 주세요', '재시술이 필요하시면',
    '사진을 보내주시면 상담실장이 확인', 'CT 촬영 결과 확인하셨죠?')에서 찾은 유형이다.
    """
    flags = []
    for sentence in (s.strip() for s in _SENTENCES.split(text or "")):
        for pattern, reason in _RECALL_RULES:
            if sentence and pattern.search(sentence):
                flags.append(_flag(sentence, reason))
    return flags


_PHONE = re.compile(r"(?<!\d)(?:0\d{1,2}|1\d{3})[-\s.)]?\d{3,4}[-\s.]?\d{4}(?!\d)")


def unknown_numbers(text: str, allowed: set[str]) -> list[dict]:
    """병원이 알려준 적 없는 전화번호. 로컬 모델이 대표번호를 '02-1234-5678'로 지어낸 일이 있었다."""
    known = {re.sub(r"\D", "", a) for a in allowed if a}
    flags, seen = [], set()
    for match in _PHONE.finditer(text or ""):
        digits = re.sub(r"\D", "", match.group(0))
        if digits not in known and digits not in seen:
            seen.add(digits)
            flags.append({
                "phrase": match.group(0),
                "reason": "병원 번호가 아니에요. AI가 지어낸 번호일 수 있으니 대표번호로 고쳐 주세요.",
                "blocking": False,
                "kind": "review",
            })
    return flags


def customer_contact_in_reply(masked_reply: str) -> list[dict]:
    """가림 토큰 상태에서 본다. 고객 번호가 답장에 있으면 대개 병원 번호와 헷갈린 것이다."""
    if re.search(r"\[(?:전화|이메일)\d+\]", masked_reply or ""):
        return [{
            "phrase": "고객 연락처",
            "reason": "고객 본인 연락처가 답장에 들어 있어요. 병원 대표번호(02-000-0000)를 쓰려던 게 아닌지 확인하세요.",
            "blocking": False,
            "kind": "review",
        }]
    return []
