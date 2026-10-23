"""발송 전 답변에서 의료광고·환자 유인 관련 위험 표현을 찾는다. 참고용 점검이며 법률 검토를 대체하지 않는다."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

_RULES = [
    (r"100\s*%|백\s*퍼센트", "효과나 안전을 수치로 보장하는 표현이에요 (의료법 제56조 위반 소지)."),
    (r"부작용\s*(?:이|은|는)?\s*(?:전혀\s*)?없", "부작용을 부정하는 표현이에요. 중요 정보 누락으로 볼 수 있어요."),
    (r"(?:통증|흉터|붓기|멍)\s*(?:이|가|은|는)?\s*(?:전혀\s*)?없", "회복 결과를 단정하는 표현이에요. 개인차가 있다고 안내해 주세요."),
    (r"완벽|무조건|보장|확실하게|틀림없", "치료 효과를 보장하는 표현이에요 (의료법 제56조 위반 소지)."),
    (r"최고|최초|유일|(?<!\d)1위|No\.?\s*1(?!\d)", "객관적 근거 없는 최상급·비교 표현이에요."),
    (r"\d[\d,]*\s*(?:만\s*)?원(?!장)", "구체적인 금액이 들어 있어요. 병원 정책상 가격은 상담 후 서면 안내예요."),
    (r"할인|이벤트|특가|무료\s*(?:시술|수술)", "환자 유인으로 볼 수 있는 표현이에요 (의료법 제27조 확인 필요)."),
    (r"전후\s*사진|후기\s*(?:보내|드릴|참고)", "치료 경험담·전후 사진 제공은 의료광고 규정을 확인해야 해요."),
    (r"\bguarantee|\bno side effects?\b|\bpainless\b|\bthe best\b|\b100 ?percent\b", "Guarantee-style claim; the same 의료법 제56조 risk applies in English."),
]
_COMPILED = [(re.compile(p, re.IGNORECASE), reason) for p, reason in _RULES]


@dataclass
class Flag:
    phrase: str
    reason: str
    blocking: bool = False  # True면 고치기 전에는 보낼 수 없다 (표현 판단이 아니라 형식 의무라서)


def check_ad_format(
    text: str, sender: str, opt_out: str | None = None, contact: str | None = None, subject: str | None = None
) -> list[dict]:
    """광고성 메시지의 표시 의무 (정보통신망법 제50조, 시행령 별표 6).

    맨 앞: '(광고)' 바로 뒤에 보낸 곳, 첫 줄에 연락처. 맨 끝: 발송 경로에 맞는 무료 수신거부 방법
    (문자는 080 번호, 브랜드 메시지는 채널 차단). 문자(LMS)는 제목도 '(광고)'로 시작해야 한다.
    opt_out·contact를 주지 않으면 위치는 보지 않고 있는지만 본다(문의함의 실시간 점검용).
    """
    flags = []
    body = (text or "").strip()
    first_line = body.splitlines()[0] if body else ""
    if not body.startswith("(광고)"):
        flags.append(Flag("(광고) 표기 없음", "광고성 메시지는 맨 앞에 '(광고)'를 표시해야 해요.", True))
    if not re.match(r"\(광고\)\s*[\[(]?" + re.escape(sender), body):
        flags.append(Flag("보낸 곳 없음", f"맨 앞 '(광고)' 바로 뒤에 보낸 곳({sender})을 밝혀야 해요.", True))
    if contact and contact not in first_line:
        flags.append(Flag("연락처 없음", f"첫 줄에 연락처({contact})를 함께 밝혀야 해요.", True))
    if opt_out and not body.endswith(opt_out):
        flags.append(Flag("수신거부 방법 없음", f"이 발송 경로의 무료 수신거부 안내 '{opt_out}'을 맨 끝에 넣어야 해요.", True))
    elif not opt_out and "수신거부" not in body:
        flags.append(Flag("수신거부 방법 없음", "수신거부 방법을 함께 안내해야 해요.", True))
    if subject is not None and not subject.lstrip().startswith("(광고)"):
        flags.append(Flag("제목 (광고) 표기 없음", "문자(LMS) 광고는 제목도 '(광고)'로 시작해야 해요.", True))
    return [asdict(f) for f in flags]


def check(text: str) -> list[dict]:
    flags: list[Flag] = []
    seen: set[str] = set()
    for pattern, reason in _COMPILED:
        for match in pattern.finditer(text or ""):
            phrase = match.group(0).strip()
            if phrase.lower() not in seen:
                seen.add(phrase.lower())
                flags.append(Flag(phrase, reason))
    return [asdict(f) for f in flags]
