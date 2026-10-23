"""문의 수집 → 고객 매칭 → 개인정보 가림 → AI 초안 → 사람 승인 → 발송·CRM 기록까지의 흐름."""
from __future__ import annotations

import difflib
import re
import threading
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from . import compliance, review
from .channels import MockChannels
from .crm import CRMAdapter, load_seed_consents, load_seed_customers
from .drafting import DraftRequest, Drafter, DrafterError
from .drafting.rules import RuleDrafter, category_of, detect_language, guess_category
from .knowledge import FOREIGN_SECTION, KnowledgeBase
from .models import CHANNEL_LABELS, Analysis, ContactLog, Customer, InboundMessage, now_iso, now_kst
from .optout import OptOutService, is_opt_out_request, opt_out_intent, result_notice
from .reply_window import reply_window
from .privacy import Masker
from .store import OPEN_STATUSES, Store

MODE_LABELS = {"local": "로컬 AI 초안 (무료)", "claude": "Claude 초안 (유료 API)", "offline": "규칙 기반 초안 (무료)"}
DRAFT_LABELS = {"local": "로컬 AI 초안", "claude": "Claude 초안", "offline": "규칙 기반 초안"}


def customer_facts(customer: Customer | None, today: date) -> dict:
    """규칙 엔진과 프롬프트가 함께 쓰는 CRM 요약. 개인 식별 정보는 넣지 않는다."""
    if customer is None:
        return {}
    facts: dict = {}
    if customer.consultations:
        c = max(customer.consultations, key=lambda x: x.date)
        facts["last_consult"] = {"date": c.date, "procedure": c.procedure, "status": c.status}
    done = [a for a in customer.appointments if a.status == "완료" and a.kind in ("수술", "시술") and a.date <= today.isoformat()]
    if done:
        a = max(done, key=lambda x: x.date)
        facts["last_procedure"] = {
            "date": a.date, "procedure": a.procedure, "days_since": (today - date.fromisoformat(a.date)).days,
        }
    upcoming = [a for a in customer.appointments if a.status == "예정" and a.date >= today.isoformat()]
    if upcoming:
        a = min(upcoming, key=lambda x: x.date)
        facts["next_appointment"] = {"date": a.date, "kind": a.kind, "procedure": a.procedure}
    return facts


_GREETING = ("안녕하세요", "Hello", "Hi ", "Dear", "こんにちは", "您好")
_OPT_OUT_TALK = re.compile(r"수신\s*거부|광고\s*(?:메시지|문자|안내)|unsubscrib|promotional", re.IGNORECASE)


def _after_greeting(reply: str, sentence: str) -> str:
    """인사가 있으면 그 뒤에, 없으면 맨 앞에 문단으로 넣는다. 인사가 본문과 한 문단에 붙어 있어도 인사 문장만 떼어 낸다."""
    first, _, rest = reply.partition("\n\n")
    if rest and len(first) <= 60:  # 짧은 인사 문단 ('오세린님, 안녕하세요. 데모성형외과 상담팀입니다.')
        greeting, body = first, ""
    else:  # 인사와 본문이 한 문단이면 첫 문장만 인사로 본다
        greeting, *body = re.split(r"(?<=[.!?。])\s+", first, maxsplit=1)
        body = body[0] if body else ""
    if len(greeting) <= 60 and any(g in greeting for g in _GREETING):
        return "\n\n".join(part for part in (greeting, sentence, body, rest) if part)
    return f"{sentence}\n\n{reply}"

class StaleThread(ValueError):
    """화면에서 본 뒤로 문의가 바뀌었다(새 메시지, 다른 곳에서 답장)."""


class Hub:
    def __init__(
        self,
        *,
        store: Store,
        crm: CRMAdapter,
        channels: MockChannels,
        knowledge: KnowledgeBase,
        drafter: Drafter,
        crm_seed: Path,
        today: date | None = None,
        clinic_name: str = "데모성형외과",
        clinic_phone: str = "02-000-0000",
    ):
        self.store = store
        self.crm = crm
        self.channels = channels
        self.knowledge = knowledge
        self.drafter = drafter
        self.fallback = RuleDrafter()
        self.crm_seed = crm_seed
        self._today = today  # 정하지 않으면 시계의 날짜를 쓴다 (밤을 넘겨 켜 둔 서버도 오늘 기준)
        self.clinic_name = clinic_name
        self.clinic_phone = clinic_phone
        self._executor = ThreadPoolExecutor(max_workers=4)
        self.clock = now_kst  # 답장 가능 시간 계산용. 테스트에서 바꿔 끼운다
        self.optout = OptOutService(crm)
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)  # 초안 만들기
        self._send_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)  # 같은 문의를 두 번 보내지 않기

    @property
    def today(self) -> date:
        return self._today or self.clock().date()

    @property
    def mode(self) -> str:
        return self.drafter.mode

    # ---- 수집 -------------------------------------------------------------

    def reset(self, wait_for_drafts: bool = True) -> dict:
        self.store.reset()
        self.channels.reset()
        self.crm.reset(load_seed_customers(self.crm_seed, self.today), load_seed_consents(self.crm_seed, self.today))
        return self.sync(wait_for_drafts=wait_for_drafts)

    def ensure_seeded(self) -> None:
        if not self.store.known_message_ids():
            self.reset(wait_for_drafts=False)
            return
        for key in self.store.stuck():  # 지난번에 초안을 만들다 꺼졌으면 다시 만든다
            self._executor.submit(self.analyze, key)

    def sync(self, wait_for_drafts: bool = True) -> dict:
        new = self.channels.fetch_new(self.store.known_message_ids())
        touched = self.ingest(new, wait_for_drafts)
        return {"fetched": len(new), "threads": len(touched), "remaining": self.channels.remaining(self.store.known_message_ids())}

    def ingest(self, messages: list[InboundMessage], wait_for_drafts: bool = True) -> list[str]:
        """새 문의를 고객과 묶어 저장하고 초안을 만든다. 데모 채널과 실제 웹훅(네이버 톡톡 등)이 같은 길을 쓴다."""
        touched: list[str] = []
        known = self.store.known_message_ids()
        for msg in messages:
            if msg.id in known:  # 웹훅이 같은 메시지를 다시 보내도 한 번만 받는다
                continue
            key, customer = self._resolve(msg)
            self.store.add_inbound(key, msg, customer.id if customer else None, customer.name if customer else msg.sender_name)
            self.optout.from_message(customer.id if customer else None, msg.channel, msg.text, msg.received_at)
            if key not in touched:
                touched.append(key)
        for key in touched:
            self.store.set_status(key, "analyzing")
        futures = [self._executor.submit(self.analyze, key) for key in touched]
        if wait_for_drafts:
            wait(futures)
        return touched


    def _resolve(self, msg: InboundMessage) -> tuple[str, Customer | None]:
        """채널 계정 → 전화번호 순으로 CRM 고객을 찾고, 같은 고객의 문의는 한 스레드로 묶는다."""
        customer = self.crm.find_by_channel(msg.channel, msg.channel_user_id)
        if customer is None and msg.phone:
            customer = self.crm.find_by_phone(msg.phone)
            if customer:
                self.crm.link_channel(customer.id, msg.channel, msg.channel_user_id)
        if customer is None:
            return f"{msg.channel}:{msg.channel_user_id}", None
        return self.store.thread_key_for_customer(customer.id) or f"cust:{customer.id}", customer

    # ---- AI 초안 ----------------------------------------------------------

    def _pending(self, messages: list[dict]) -> list[dict]:
        last_out = max((i for i, m in enumerate(messages) if m["direction"] == "out"), default=-1)
        return [m for m in messages[last_out + 1 :] if m["direction"] == "in"]

    def build_request(self, key: str) -> tuple[DraftRequest, Masker]:
        thread = self.store.thread(key)
        messages = self.store.messages(key)
        pending = self._pending(messages) or [m for m in messages if m["direction"] == "in"][-1:]
        customer = self.crm.get(thread["customer_id"]) if thread["customer_id"] else None
        facts = customer_facts(customer, self.today)

        senders = [m["sender_name"] for m in messages]
        if customer:  # 채널마다 다르게 적힌 보낸 사람 이름은 CRM 고객과 같은 사람이다
            masker = Masker([customer.name], aliases={s: customer.name for s in senders})
        else:
            masker = Masker(senders)
        if customer:
            token = masker.token_for("고객", customer.name)
        elif pending[-1]["channel"] == "web":  # 홈페이지 상담폼은 실명을 받는다
            token = masker.token_for("고객", pending[-1]["sender_name"])
        else:
            token = ""

        query = " ".join(m["text"] for m in pending)
        category = guess_category(query, facts)
        # 메시지마다 따로 찾는다. 질문 여러 개를 합쳐서 찾으면 한쪽 질문의 자료를 놓친다.
        # 여러 메시지면 각 메시지에 시술 단서가 있을 때만 그 분야를 우대한다 ('토요일 상담' 질문에 코성형 자료가 끼지 않게).
        chunks: list = []
        for m in pending:
            hint = category if len(pending) == 1 else category_of(m["text"])
            for chunk in self.knowledge.search(m["text"], k=3 if len(pending) == 1 else 2, category=hint):
                if chunk not in chunks:
                    chunks.append(chunk)
        chunks = chunks[:5]
        if detect_language(query) != "ko" and (foreign := self.knowledge.by_section(FOREIGN_SECTION)):
            chunks.append(foreign)
        chunks += self.knowledge.pinned()

        profile = []
        if customer:
            profile.append(f"고객: {token} · 기존 고객 · 선호 언어 {customer.language} · 태그: {', '.join(customer.tags) or '없음'}")
            profile += [f"상담 이력: {c.date} {c.procedure} · {c.counselor} · {c.status}" + (f" · 메모: {c.note}" if c.note else "")
                        for c in customer.consultations]
            profile += [f"예약/수술: {a.date}{' ' + a.time if a.time else ''} {a.kind} {a.procedure} ({a.status})" for a in customer.appointments]
            profile += [f"이전 응대: {log.at[:10]} {log.summary}" for log in self.crm.contact_logs(customer.id)[-2:]]
            if "last_procedure" in facts:
                profile.append(f"최근 수술·시술 후 {facts['last_procedure']['days_since']}일째")
        elif token:
            profile.append(f"고객: {token} · CRM 미등록 신규 문의")
        if customer and any(is_opt_out_request(m["text"]) for m in pending):
            profile.append("안내: 이 문의의 광고 수신거부 요청은 시스템이 CRM에 이미 반영했다. 처리 안내 문장은 시스템이 답장 앞에 붙이니 "
                           "수신거부 이야기는 쓰지 말고 나머지 질문에만 답한다.")

        request = DraftRequest(
            today=self.today.isoformat(),
            clinic_name=self.clinic_name,
            reply_channel=CHANNEL_LABELS[pending[-1]["channel"]],
            customer_token=token,
            registered=customer is not None,
            profile_lines=[masker.mask(line) for line in profile],
            facts=facts,
            messages=[
                {"at": m["at"].replace("T", " "), "channel": CHANNEL_LABELS[m["channel"]], "text": masker.mask(m["text"])}
                for m in pending
            ],
            knowledge=chunks,
        )
        return request, masker

    def analyze(self, key: str) -> None:
        with self._locks[key]:
            try:
                request, masker = self.build_request(key)
                note = ""
                mode = self.drafter.mode
                try:
                    analysis = self.drafter.draft(request)
                    if mode == "local" and analysis.urgency == "high":  # 로컬 모드의 응급 문의는 모델을 부르지 않고 고정 문구로 쓴다
                        mode = self.fallback.mode
                except DrafterError as e:
                    analysis = self.fallback.draft(request)
                    mode = self.fallback.mode
                    note = f"{e} 규칙 기반 초안으로 대신했어요."
                optout = self._optout_notice(key)
                repeated = bool(optout) and mode != "offline" and bool(_OPT_OUT_TALK.search(analysis.draft_reply + analysis.draft_reply_ko))
                if optout:  # 처리 결과 안내는 지어내지 않게 코드가 붙인다 (광고 앞뒤 법정 문구와 같은 방식)
                    reply_notice, ko_notice = optout
                    if analysis.draft_reply_ko and analysis.draft_reply_ko != analysis.draft_reply:
                        analysis.draft_reply_ko = _after_greeting(analysis.draft_reply_ko, ko_notice)
                    analysis.draft_reply = _after_greeting(analysis.draft_reply, reply_notice)
                contact_flags = review.customer_contact_in_reply(analysis.draft_reply + "\n" + analysis.draft_reply_ko)
                for field in ("draft_reply", "draft_reply_ko", "summary"):
                    setattr(analysis, field, masker.unmask(getattr(analysis, field)))
                texts = "\n".join({analysis.draft_reply_ko, analysis.draft_reply})
                if mode == "offline":
                    flags = compliance.check(texts)
                else:  # 고객 본인 번호는 contact_flags가 따로 짚으므로 여기서는 병원·고객 번호 말고 지어낸 번호만 본다
                    allowed = {self.clinic_phone, "+82-2-000-0000", *masker.values("전화")}
                    flags = compliance.check(texts) + review.check(texts) + contact_flags + review.unknown_numbers(texts, allowed)
                flags += self._optout_flags(key, bool(optout))
                if repeated:
                    flags.append({"phrase": "수신거부 이야기 중복", "kind": "review", "blocking": False,
                                  "reason": "AI가 본문에서 수신거부를 또 말했어요. 처리 결과 안내 문단과 겹치거나 어긋나면 지워 주세요."})
                self.store.save_analysis(key, analysis, flags, request.render_prompt(), masker.replacements, mode, note)
            except Exception as e:  # 한 스레드가 실패해도 문의함 전체는 계속 돌아야 한다
                self.store.set_status(key, "error", f"초안을 만들지 못했어요: {e}")

    def _optout_flags(self, key: str, notice_added: bool) -> list[dict]:
        """수신거부 이야기가 있는 문의를 상담실장이 놓치지 않게 한다."""
        thread = self.store.thread(key)
        pending = self._pending(self.store.messages(key))
        intents = {opt_out_intent(m["text"]) for m in pending} - {None}
        if not intents:
            return []
        flag = {"kind": "review", "blocking": False}
        customer = self.crm.get(thread["customer_id"]) if thread["customer_id"] else None
        if "request" in intents and customer is None:
            return [{**flag, "phrase": "수신거부 요청 · CRM 고객 못 찾음",
                     "reason": "이 문의자를 CRM에서 찾지 못해 동의를 바꾸지 못했어요. CRM에 같은 사람이 있으면 그 고객의 광고 수신 동의를 꺼 주세요."}]
        if "request" in intents and notice_added:
            refusal = self._latest_refusal(customer.id)
            first = min(m["at"] for m in pending if opt_out_intent(m["text"]) == "request")
            if refusal and refusal.at < first:
                when = datetime.fromisoformat(refusal.at)
                reason = f"이미 {when.month}/{when.day} {refusal.source}로 거부돼 있어요. 인사 다음 문단에 그 날짜로 처리 결과를 넣었어요."
            else:
                reason = "CRM 광고 수신 동의를 '아니오'로 바꿨어요. 인사 다음 문단이 처리 결과 안내예요."
            return [{**flag, "phrase": "수신거부 요청", "reason": reason}]
        if "unclear" in intents:
            return [{**flag, "phrase": "수신거부 관련 문의",
                     "reason": "거부 요청인지 확실하지 않아(해제·부인·방법 문의 등) CRM은 그대로 뒀어요. 고객 뜻을 확인한 뒤 필요하면 CRM 패널의 '수신거부 반영'을 눌러 주세요."}]
        return []

    def _latest_refusal(self, customer_id: str):
        refusals = [e for e in self.crm.consent_history(customer_id) if e.field == "marketing_consent" and not e.value]
        return refusals[-1] if refusals else None

    def _optout_notice(self, key: str) -> tuple[str, str] | None:
        """답하지 않은 메시지에 수신거부 요청이 있고 CRM에 반영됐으면, 답장 첫머리에 붙일 처리 결과 안내 (고객 언어, 한국어).

        수신거부를 처리하면 보낸 쪽 이름·처리 날짜·처리 내용을 고객에게 알려야 한다. 답장이 그 안내를 겸한다.
        """
        thread = self.store.thread(key)
        customer = self.crm.get(thread["customer_id"]) if thread["customer_id"] else None
        requests = [m for m in self._pending(self.store.messages(key)) if is_opt_out_request(m["text"])]
        if not customer or not requests or customer.marketing_consent:
            return None
        refusal = self._latest_refusal(customer.id)  # 안내에는 거부가 실제로 반영된 날을 쓴다
        at = refusal.at if refusal else requests[-1]["at"]
        return result_notice(self.clinic_name, at, customer.language), result_notice(self.clinic_name, at)

    # ---- 승인·발송 --------------------------------------------------------

    def approve(self, key: str, final_text: str, handled_by: str = "상담실장", last_seen: str | None = None) -> dict:
        """last_seen: 상담실장이 화면에서 본 마지막 고객 메시지 ID. 그 뒤로 새 메시지가 왔거나 이미 답했으면 보내지 않는다."""
        with self._send_locks[key]:
            return self._approve(key, final_text, handled_by, last_seen)

    def _approve(self, key: str, final_text: str, handled_by: str, last_seen: str | None) -> dict:
        thread = self.store.thread(key)
        messages = self.store.messages(key)
        pending = self._pending(messages)
        if not pending:
            raise StaleThread("이미 답장한 문의예요(다른 화면이나 채널 관리 화면에서 보냈을 수 있어요). 새로 고쳐 확인해 주세요.")
        if last_seen and pending[-1]["id"] != last_seen:
            raise StaleThread("그사이 고객이 새 메시지를 보냈어요. 새 메시지를 확인한 뒤 다시 보내 주세요.")
        inbound = [m for m in messages if m["direction"] == "in"]
        last = (self._pending(messages) or inbound)[-1]
        analysis = Analysis(**thread["analysis"]) if thread["analysis"] else None
        window = self._reply_window(messages)
        if window.state == "closed":
            raise ValueError(window.message)

        # 먼저 보낸다. 보내기가 실패하면(톡톡 API 오류 등) CRM에 리드나 응대 기록을 남기지 않는다.
        sent = self.channels.send(last["channel"], last["channel_user_id"], final_text, tag=window.tag, reply_to=last["id"])
        customer = self.crm.get(thread["customer_id"]) if thread["customer_id"] else None
        if customer is None:
            first = inbound[0]
            customer = self.crm.create_lead(
                name=first["sender_name"],
                phone=next((m["phone"] for m in inbound if m["phone"]), ""),
                email=next((m["email"] for m in inbound if m["email"]), ""),
                language=analysis.language if analysis else "ko",
                channel=first["channel"],
                channel_user_id=first["channel_user_id"],
            )
        for m in inbound:
            if customer.channel_ids.get(m["channel"]) != m["channel_user_id"]:
                self.crm.link_channel(customer.id, m["channel"], m["channel_user_id"])
                customer.channel_ids[m["channel"]] = m["channel_user_id"]

        self.crm.log_contact(
            customer.id,
            ContactLog(
                at=sent["at"],
                channel=CHANNEL_LABELS[last["channel"]],
                summary=analysis.summary if analysis else "",
                reply=final_text,
                handled_by=handled_by,
            ),
        )
        similarity = difflib.SequenceMatcher(None, analysis.draft_reply, final_text).ratio() if analysis else 0.0
        self.store.add_outbound(key, f"out-{uuid.uuid4().hex[:8]}", last["channel"], last["channel_user_id"], final_text, sent["at"])
        self.store.mark_sent(key, final_text, round(similarity, 3), customer.id)
        return {"sent": sent, "customer_id": customer.id, "similarity": round(similarity, 3)}

    def record_agent_reply(self, channel: str, channel_user_id: str, text: str, manager: str, at: str | None = None) -> str | None:
        """채널 관리 화면(네이버 톡톡 파트너센터 등)에서 상담원이 직접 답한 것을 문의함과 CRM에 맞춰 둔다."""
        customer = self.crm.find_by_channel(channel, channel_user_id)
        key = (self.store.thread_key_for_customer(customer.id) if customer else None) or f"{channel}:{channel_user_id}"
        if self.store.thread(key) is None:
            return None
        at = at or now_iso()
        self.store.add_outbound(key, f"out-{uuid.uuid4().hex[:8]}", channel, channel_user_id, text, at)
        self.store.set_status(key, "sent", f"{CHANNEL_LABELS[channel]} 관리 화면에서 {manager}님이 직접 답장했어요.")
        if customer:
            self.crm.log_contact(customer.id, ContactLog(at=at, channel=CHANNEL_LABELS[channel], summary="[직접 답장] 채널 관리 화면에서 보냄",
                                                         reply=text, handled_by=manager))
        return key

    def _reply_window(self, messages: list[dict]):
        """답장이 갈 채널(가장 최근 문의의 채널)에서, 그 채널의 마지막 고객 메시지 기준으로 계산한다."""
        inbound = [m for m in messages if m["direction"] == "in"]
        if not inbound:
            return reply_window("", "", self.clock())
        channel = (self._pending(messages) or inbound)[-1]["channel"]
        last_at = max(m["at"] for m in inbound if m["channel"] == channel)
        return reply_window(channel, last_at, self.clock())

    def escalate(self, key: str, note: str, handled_by: str = "상담실장") -> None:
        thread = self.store.thread(key)
        self.store.set_status(key, "escalated", note)
        if thread["customer_id"]:
            summary = thread["analysis"]["summary"] if thread["analysis"] else ""
            self.crm.log_contact(
                thread["customer_id"],
                ContactLog(at=now_iso(), channel="내부 이관", summary=f"[담당자 확인 요청] {summary}", reply=note, handled_by=handled_by),
            )

    # ---- 화면용 -----------------------------------------------------------

    def thread_list(self) -> list[dict]:
        items = []
        for t in self.store.threads():
            messages = self.store.messages(t["key"])
            inbound = [m for m in messages if m["direction"] == "in"]
            a = t["analysis"] or {}
            items.append({
                "key": t["key"],
                "name": t["display_name"],
                "registered": bool(t["customer_id"]),
                "channels": list(dict.fromkeys(m["channel"] for m in inbound)),
                "last_text": inbound[-1]["text"] if inbound else "",
                "last_at": t["last_at"],
                "status": t["status"],
                "category": a.get("category"),
                "intent": a.get("intent"),
                "urgency": a.get("urgency"),
                "needs_human": a.get("needs_human", False),
                "summary": a.get("summary"),
                "message_count": len(inbound),
            })
        items.sort(key=lambda x: x["last_at"], reverse=True)
        items.sort(key=lambda x: (x["status"] not in OPEN_STATUSES, x["urgency"] != "high"))
        return items

    def thread_view(self, key: str) -> dict | None:
        thread = self.store.thread(key)
        if thread is None:
            return None
        customer = self.crm.get(thread["customer_id"]) if thread["customer_id"] else None
        sources = []
        if thread["analysis"]:
            for chunk_id in thread["analysis"]["used_sources"]:
                if chunk := self.knowledge.get(chunk_id):
                    sources.append(asdict(chunk))
        return {
            **thread,
            "messages": self.store.messages(key),
            "customer": asdict(customer) if customer else None,
            "contact_logs": [asdict(log) for log in self.crm.contact_logs(customer.id)] if customer else [],
            "consent_history": [asdict(e) for e in self.crm.consent_history(customer.id)] if customer else [],
            "sources": sources,
            "mode_label": DRAFT_LABELS.get(thread["mode"] or "", ""),
            "reply_window": self._reply_window(self.store.messages(key)).to_dict(),
            # 실제 웹훅으로 들어온 대화면 승인한 답장이 실제로 나간다 (데모 시드 문의는 outbox에만 남는다)
            "live": any(m["id"].startswith("talktalk:") for m in self.store.messages(key)),
        }

    def status(self) -> dict:
        return {
            "mode": self.mode,
            "mode_label": MODE_LABELS[self.mode] + (f" · {self.drafter.model}" if self.mode == "local" else ""),
            "crm": self.crm.name,
            "crm_description": self.crm.description,
            "clinic": self.clinic_name,
            "metrics": self.store.metrics(),
            "remaining": self.channels.remaining(self.store.known_message_ids()),
        }
