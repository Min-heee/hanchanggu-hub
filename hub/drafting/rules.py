"""API 키 없이도 돌아가는 규칙 기반 분류·초안. Claude 호출이 실패했을 때의 대체 경로이기도 하다."""
from __future__ import annotations

import re
from datetime import date

from ..knowledge import bigrams
from ..models import Analysis, KnowledgeChunk
from .base import DraftRequest

_TOKEN = re.compile(r"\[[^\]\d]+\d+\]")

URGENT = re.compile(
    r"피가|출혈|열이|고열|3[89](?:\.\d)?\s*도|고름|숨\s*쉬기|호흡|점점\s*(?:심해|부어)"
    r"|bleed|fever|pus\b|can'?t breathe|出血|発熱|熱が|发烧|流血",
    re.IGNORECASE,
)

CATEGORY_KEYWORDS = {
    "코성형": ["코성형", "코 ", "코끝", "콧대", "부목", "매부리", "비중격", "rhinoplasty", "nose", "鼻"],
    "눈성형": ["쌍꺼풀", "눈매", "트임", "눈 ", "라인", "eyelid", "二重", "双眼皮"],
    "안면윤곽": ["사각턱", "광대", "턱끝", "윤곽", "jaw", "cheekbone", "輪郭", "エラ", "颧骨"],
    "가슴성형": ["가슴", "breast", "豊胸", "隆胸"],
    "지방흡입": ["지방흡입", "지방 흡입", "liposuction", "脂肪吸引", "吸脂"],
    "리프팅·쁘띠": ["필러", "보톡스", "리프팅", "filler", "botox", "lifting", "ヒアルロン", "ボトックス", "玻尿酸"],
}

# 앞에 있을수록 우선한다. '이상 증상'은 URGENT 정규식으로 따로 판단한다.
INTENT_KEYWORDS = [
    ("불만·클레임", ["실망", "불만", "화가", "짜증", "기다리다", "항의", "환불", "disappointed", "complain", "不満"]),
    ("가격 문의", ["가격", "비용", "얼마", "견적", "price", "cost", "how much", "料金", "値段", "いくら", "价格", "多少钱"]),
    ("예약·일정", ["예약", "상담 받", "상담 한", "가능한가", "가능할까", "되나요", "토요일", "일정", "book", "appointment", "available", "予約", "相談", "预约"]),
    ("회복·경과", ["붓기", "멍", "회복", "실밥", "부목", "쉬어야", "복귀", "찜질", "recovery", "swelling", "stay", "ダウンタイム", "腫れ", "恢复"]),
    ("시술 상담", ["상담", "재수술", "consult", "カウンセリング", "咨询"]),
]

LANGUAGE_NAMES = {"ko": "한국어", "en": "영어", "ja": "일본어", "zh": "중국어", "other": "기타 언어"}

CLOSING_KO = {
    "가격 문의": "비용은 얼굴 상태와 수술 방법에 따라 달라져서 메신저로는 금액을 안내하지 않고, 원장 상담 후 개인별 견적을 서면으로 드리고 있어요. 편하신 날짜를 알려주시면 상담 예약을 도와드릴게요.",
    "예약·일정": "희망하시는 날짜와 시간대를 2~3개 알려주시면 가능한 시간을 확인해서 바로 안내드릴게요.",
    "회복·경과": "회복 속도는 사람마다 차이가 있어요. 걱정되는 부분이 있으면 사진을 보내주시거나 내원하실 때 꼭 말씀해 주세요.",
    "시술 상담": "정확한 방법은 원장 진료 후 결정되니, 편하신 날짜에 상담 예약을 잡아드릴게요.",
    "기타": "더 궁금하신 점이 있으면 편하게 말씀해 주세요.",
}

URGENT_KO = (
    "말씀하신 증상은 바로 확인이 필요해요. 메신저 답장을 기다리지 마시고 지금 병원 대표번호(02-000-0000)로 전화 주세요. "
    "진료 시간 외에는 가까운 응급실을 먼저 방문해 주세요. 담당 간호팀에도 바로 전달해 두겠습니다."
)
COMPLAINT_KO = "불편을 드려 정말 죄송합니다. 말씀해 주신 내용은 담당 실장이 직접 확인한 뒤 오늘 안에 연락드리겠습니다."

FOREIGN = {
    "en": {
        "greeting": "Hello{name}, thank you for contacting Demo Plastic Surgery.",
        "name": " {token}",
        "followup": "Thank you for getting back to us after your consultation.",
        "body": "We offer consultations in English, and an interpreter coordinator joins you for the consultation and on the day of surgery. Please send us two or three preferred dates and times, and we will confirm availability. How long you need to stay after surgery depends on the procedure, and the doctor will explain it during your consultation.",
        "price": "Costs depend on your condition and the surgical method, so we provide a written quote after your consultation with the doctor.",
        "urgent": "Please do not wait for a message reply. Call our main number (+82-2-000-0000) right away, or visit the nearest emergency room outside clinic hours.",
        "sign": "Best regards,\nDemo Plastic Surgery Consultation Team",
    },
    "ja": {
        "greeting": "{name}こんにちは。デモ美容外科カウンセリングチームです。",
        "name": "{token}様、",
        "followup": "カウンセリング後に改めてご連絡いただきありがとうございます。",
        "body": "日本語の通訳コーディネーターがカウンセリングと手術当日に同行いたします。ご希望の日程を2〜3つお知らせいただければ、空き状況を確認してご案内いたします。手術後の滞在期間は施術内容によって異なるため、カウンセリング時にご説明いたします。",
        "price": "費用はお顔の状態や手術方法によって異なるため、院長カウンセリング後に書面でお見積りをお渡ししております。",
        "urgent": "メッセージの返信を待たずに、すぐに代表番号（+82-2-000-0000）へお電話ください。診療時間外は最寄りの救急外来を受診してください。",
        "sign": "デモ美容外科カウンセリングチーム",
    },
    "zh": {
        "greeting": "{name}您好，这里是演示整形外科咨询团队。",
        "name": "{token}，",
        "followup": "感谢您在面诊后再次联系我们。",
        "body": "我们提供中文翻译协调员，可陪同面诊和手术当天。请告诉我们两到三个您方便的日期和时间，我们确认后会尽快回复。术后需要停留的时间因手术而异，医生会在面诊时说明。",
        "price": "费用会根据您的面部状况和手术方式而不同，我们会在院长面诊后提供书面报价。",
        "urgent": "请不要等待消息回复，立即拨打医院总机（+82-2-000-0000）。非诊疗时间请先前往最近的急诊室。",
        "sign": "演示整形外科咨询团队",
    },
}
FOREIGN_KO = {
    "greeting": "{name}안녕하세요, 데모성형외과입니다.",
    "followup": "상담 후 다시 연락 주셔서 감사합니다.",
    "body": "통역 코디네이터가 상담과 수술 당일에 함께합니다. 희망 날짜와 시간대를 2~3개 알려주시면 확인 후 안내드리겠습니다. 수술 후 체류 기간은 수술 종류에 따라 달라 상담 때 원장님이 설명드립니다.",
    "price": "비용은 얼굴 상태와 수술 방법에 따라 달라 원장 상담 후 서면 견적을 드립니다.",
    "urgent": URGENT_KO,
    "sign": "데모성형외과 상담팀 드림",
}


def detect_language(text: str) -> str:
    text = _TOKEN.sub("", text)
    if re.search(r"[぀-ヿ]", text):
        return "ja"
    if re.search(r"[가-힣]", text):
        return "ko"
    if re.search(r"[一-鿿]", text):
        return "zh"
    if re.search(r"[A-Za-z]", text):
        return "en"
    return "other"


def category_of(text: str) -> str | None:
    lowered = text.lower() + " "
    scores = {cat: sum(lowered.count(k.lower()) for k in words) for cat, words in CATEGORY_KEYWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] else None


def guess_category(text: str, facts: dict) -> str:
    """문의 내용으로 먼저 판단하고, 단서가 없으면 CRM의 최근 시술·상담으로 판단한다."""
    found = category_of(text)
    if found:
        return found
    for key in ("last_procedure", "last_consult"):
        if key in facts and (cat := category_of(facts[key]["procedure"])):
            return cat
    return "기타"


def guess_intent(text: str) -> str:
    if URGENT.search(text):
        return "이상 증상"
    lowered = text.lower()
    for intent, words in INTENT_KEYWORDS:
        if any(w.lower() in lowered for w in words):
            return intent
    return "기타"


def needs_human(text: str, intent: str) -> tuple[bool, str]:
    if intent == "이상 증상":
        return True, "수술 후 응급일 수 있는 증상이에요. 간호팀이 바로 전화로 확인해야 해요."
    if intent == "불만·클레임":
        return True, "불만 접수 건이에요. 실장이 직접 사과하고 경위를 확인해야 해요."
    if "재수술" in text or "다른 병원" in text:
        return True, "재수술·타병원 수술 이력은 원장 진료 판단이 필요해요."
    return False, ""


def _month_day(iso: str) -> str:
    d = date.fromisoformat(iso[:10])
    return f"{d.month}월 {d.day}일"


def history_sentence(facts: dict, intent: str, today: str) -> str:
    parts = []
    last = facts.get("last_procedure")
    if last and last["days_since"] <= 60 and intent in ("회복·경과", "이상 증상"):
        parts.append(f"{last['procedure']} 받으신 지 {last['days_since']}일째시네요.")
        nxt = facts.get("next_appointment")
        if nxt:
            when = "내일" if (date.fromisoformat(nxt["date"]) - date.fromisoformat(today)).days == 1 else _month_day(nxt["date"])
            parts.append(f"{when} {nxt['procedure']} 예약도 잡혀 있어요.")
    elif facts.get("last_consult") and intent in ("예약·일정", "가격 문의", "시술 상담", "회복·경과"):
        consult = facts["last_consult"]
        parts.append(f"지난 {_month_day(consult['date'])} {consult['procedure']} 상담 이후 다시 연락 주셔서 감사합니다.")
    return " ".join(parts)


def best_sentences(query: str, chunks: list[KnowledgeChunk], limit: int = 2) -> tuple[str, list[str]]:
    """가장 가까운 자료에서 문의와 겹치는 문장을 골라 원래 순서대로 이어 붙인다."""
    if not chunks:
        return "", []
    chunk = chunks[0]
    q = bigrams(query)
    sentences = [s for s in re.split(r"(?<=[.!?。])\s+", chunk.text) if s]
    ranked = sorted(range(len(sentences)), key=lambda i: len(q & bigrams(sentences[i])), reverse=True)[:limit]
    return " ".join(sentences[i] for i in sorted(ranked)), [chunk.id]


class RuleDrafter:
    mode = "offline"

    def draft(self, request: DraftRequest) -> Analysis:
        text = request.query
        language = detect_language(text)
        category = guess_category(text, request.facts)
        intent = guess_intent(text)
        urgent = intent == "이상 증상"
        human, reason = needs_human(text, intent)
        topical = [c for c in request.knowledge if c.category != "공통"] or request.knowledge

        if language in FOREIGN:
            reply, reply_ko, sources = self._foreign(request, language, intent, urgent)
            summary = f"{LANGUAGE_NAMES[language]} 문의 · {category} {intent}"
        else:
            reply, sources = self._korean(request, intent, urgent, topical)
            reply_ko = reply
            snippet = _TOKEN.sub("", request.messages[-1]["text"]).strip()
            summary = f"{category} · {intent} — {snippet[:40]}{'…' if len(snippet) > 40 else ''}"

        return Analysis(
            category=category,
            intent=intent,
            urgency="high" if urgent else ("low" if intent == "기타" else "normal"),
            language=language,
            summary=summary,
            needs_human=human,
            needs_human_reason=reason,
            draft_reply=reply,
            draft_reply_ko=reply_ko,
            used_sources=sources,
        )

    def _korean(self, request: DraftRequest, intent: str, urgent: bool, chunks: list[KnowledgeChunk]) -> tuple[str, list[str]]:
        name = f"{request.customer_token}님, " if request.customer_token else ""
        parts = [f"{name}안녕하세요. {request.clinic_name} 상담팀입니다."]
        if urgent:
            pinned = [c.id for c in request.knowledge if "응급" in c.title]
            return "\n\n".join(parts + [URGENT_KO, f"{request.clinic_name} 상담팀 드림"]), pinned
        if context := history_sentence(request.facts, intent, request.today):
            parts.append(context)
        if intent == "불만·클레임":
            return "\n\n".join(parts + [COMPLAINT_KO, f"{request.clinic_name} 상담팀 드림"]), []
        body, sources = best_sentences(request.query, chunks)
        if body:
            parts.append(body)
        parts.append(CLOSING_KO.get(intent, CLOSING_KO["기타"]))
        parts.append(f"{request.clinic_name} 상담팀 드림")
        return "\n\n".join(parts), sources

    def _foreign(self, request: DraftRequest, language: str, intent: str, urgent: bool) -> tuple[str, str, list[str]]:
        t = FOREIGN[language]
        name = t["name"].format(token=request.customer_token) if request.customer_token else ""
        name_ko = f"{request.customer_token}님, " if request.customer_token else ""
        keys = ["urgent"] if urgent else (["followup"] if request.facts.get("last_consult") else []) + (["price", "body"] if intent == "가격 문의" else ["body"])
        reply = [t["greeting"].format(name=name)] + [t[k] for k in keys] + [t["sign"]]
        reply_ko = [FOREIGN_KO["greeting"].format(name=name_ko)] + [FOREIGN_KO[k] for k in keys] + [FOREIGN_KO["sign"]]
        sources = [c.id for c in request.knowledge if "외국인" in c.title]
        return "\n\n".join(reply), "\n\n".join(reply_ko), sources
