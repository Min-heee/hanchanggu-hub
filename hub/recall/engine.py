"""리콜 흐름: CRM 훑기 → 규칙에 걸린 고객 찾기 → 보내면 안 되는 경우 거르기 → 메시지 준비 → 실장 승인 → 발송·CRM 기록.

AI는 개인화가 필요한 규칙에만 쓴다. 알림톡은 검수된 템플릿에 변수만 넣을 수 있어 코드로 채운다.
발송 경로·시간·글자 수·표시 규칙은 delivery.py에 모아 두었다.

승인은 '예약 발송'이다. 발송 시각이 지났으면 바로 보내고, 아직이면(아침 10시 전 승인, 밤에 승인한 광고) 예약으로 두었다가
보내기 직전에 수신 동의·채널 차단·답하지 않은 문의를 다시 확인한다. 예약은 보내기 전까지 취소할 수 있다.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import asdict
from datetime import date, datetime

from .. import compliance, review
from ..crm import CRMAdapter
from ..drafting import DrafterError
from ..drafting.rules import category_of
from ..knowledge import KnowledgeBase
from ..models import ConsentEvent, ContactLog, Customer, now_kst
from ..optout import mentions_result_notice
from ..privacy import Masker
from ..store import OPEN_STATUSES, Store
from . import delivery
from .delivery import CHANNELS, SEND_TIME
from .rules import RecallRule, fill_template, find_hits, next_visit_sentence, unfilled
from .store import RecallStore


def mask_recipient(recipient: str) -> str:
    digits = "".join(ch for ch in recipient if ch.isdigit())
    return f"{digits[:3]}-****-{digits[-4:]}" if len(digits) >= 8 else recipient


def _minutes(moment: datetime) -> str:
    return moment.isoformat(timespec="minutes")


class RecallEngine:
    def __init__(
        self,
        *,
        crm: CRMAdapter,
        inbox: Store,
        channels,
        knowledge: KnowledgeBase,
        rules: list[RecallRule],
        store: RecallStore,
        writer=None,
        today: date | None = None,
        clinic_name: str = "데모성형외과",
        clinic_phone: str = "02-000-0000",
    ):
        self.crm = crm
        self.inbox = inbox
        self.channels = channels
        self.knowledge = knowledge
        # 정보성 규칙을 먼저 돌려야 '하루 1건' 제한에서 진료 안내가 광고에 밀리지 않는다.
        self.rules = sorted(rules, key=lambda r: r.is_ad)
        self.rules_by_id = {r.id: r for r in rules}
        self.store = store
        self.writer = writer
        self._today = today  # 정하지 않으면 시계의 날짜
        self.clinic_name = clinic_name
        self.clinic_phone = clinic_phone
        self.clock = now_kst  # 한국 시간. 테스트에서 밤 시간 등을 흉내 낼 수 있게 바꿔 끼운다
        self._executor = ThreadPoolExecutor(max_workers=4)

    @property
    def today(self) -> date:
        return self._today or self.clock().date()

    @property
    def mode(self) -> str:
        return self.writer.mode if self.writer else "offline"

    def reset(self) -> None:
        self.store.reset()

    def _now(self) -> datetime:
        return self.clock().replace(second=0, microsecond=0)

    def _open_threads(self) -> dict[str, dict]:
        return {t["customer_id"]: t for t in self.inbox.threads() if t["customer_id"] and t["status"] in OPEN_STATUSES}

    # ---- 대상 찾기 ----------------------------------------------------------

    def plan(self, target: date, wait_for_drafts: bool = True) -> dict:
        day = target.isoformat()
        self.store.clear_blocked(day)
        open_threads = self._open_threads()
        # 그날 계획된 건과, 다른 날 계획됐지만 그날 나가게 된 예약(밤에 승인돼 미뤄진 광고 등)을 함께 센다.
        contacted_today = {i["customer_id"] for i in self.store.items_for(day) if i["status"] != "blocked"}
        contacted_today |= self.store.customers_sending_on(day)
        created, blocked, to_draft = 0, 0, []

        for rule in self.rules:
            legal = rule.trigger.startswith("consent")  # 법정 기한이 있는 수신 동의 안내
            for customer in self.crm.all_customers():
                consents = self.crm.consent_history(customer.id) if legal else []
                for hit in find_hits(rule, customer, target, consents):
                    unsent = self.store.unsent_item(customer.id, rule.id, hit.anchor) if legal else None
                    if rule.trigger == "consent_changed" and self._notified(customer.id, hit.values["요청시각"]):
                        if unsent:  # 문의함 답장에 같은 안내를 넣어 보냈다
                            self.store.update(unsent["id"], status="skipped", note="문의함 답장으로 이미 알렸어요.")
                        continue
                    if unsent and unsent["date"] != day:
                        self.store.update(unsent["id"], date=day)  # 승인하지 못한 법정 안내는 기한까지 다음 날로 넘긴다
                        contacted_today.add(customer.id)
                        continue
                    if self.store.exists(customer.id, rule.id, hit.anchor):
                        continue
                    if rule.is_ad:  # 고객 목록을 읽은 뒤 들어온 수신거부도 놓치지 않게 다시 읽는다
                        customer = self.crm.get(customer.id) or customer
                    channel, recipient, route_problem = delivery.route(rule, customer)
                    reason = hit.blocked or self._block_reason(
                        rule, customer, route_problem, open_threads.get(customer.id), customer.id in contacted_today
                    )
                    item = {
                        "date": day, "customer_id": customer.id, "customer_name": customer.name, "rule_id": rule.id,
                        "anchor": hit.anchor, "reason": hit.reason, "channel": channel or "", "recipient": recipient,
                        "scheduled_at": _minutes(datetime.combine(target, SEND_TIME)),
                        "block_reason": reason or "",
                    }
                    if reason:
                        self.store.insert({**item, "status": "blocked"})
                        blocked += 1
                        continue
                    if rule.delivery == "template":
                        text = fill_template(rule.template, self._values(customer, hit.values, target, channel))
                        flags = self._flags(rule, channel, text) + self._overlap_flags(rule, open_threads.get(customer.id))
                        item_id = self.store.insert({**item, "status": "ready", "text": text, "flags": flags, "mode": "template"})
                    else:
                        item_id = self.store.insert({**item, "status": "drafting"})
                        to_draft.append(item_id)
                    if rule.is_ad and not (self.crm.get(customer.id) or customer).marketing_consent:
                        # 넣는 사이에 수신거부가 들어왔다 (리스너는 아직 없던 항목을 멈출 수 없었다)
                        self.store.update(item_id, status="blocked", block_reason="수신거부 반영: 계획하는 사이에 들어온 거부")
                        blocked += 1
                        continue
                    contacted_today.add(customer.id)
                    created += 1

        futures = [self._executor.submit(self.draft, item_id) for item_id in to_draft]
        if wait_for_drafts:
            wait(futures)
        return {"date": day, "created": created, "blocked": blocked}

    def _block_reason(
        self, rule: RecallRule, customer: Customer, route_problem: str | None, thread: dict | None, already_today: bool
    ) -> str | None:
        if rule.trigger == "consent_anniversary" and not customer.marketing_consent:
            return "광고 수신 동의를 철회해서 2년 확인 안내는 보내지 않아요."
        if rule.delivery == "template" and customer.language != "ko":
            return "외국어 알림톡 템플릿이 없어요. 담당 코디네이터가 직접 안내해 주세요."
        if route_problem:
            return route_problem
        if rule.is_ad and not customer.marketing_consent:
            refused = [e for e in self.crm.consent_history(customer.id) if e.field == "marketing_consent"]
            if refused and not refused[-1].value:
                when = datetime.fromisoformat(refused[-1].at)
                return f"수신거부 반영: {refused[-1].source} ({when.month}/{when.day}). 광고는 보내지 않아요."
            return "광고성 정보 수신 동의가 없어요 (정보통신망법 제50조)."
        if thread:
            urgent = (thread["analysis"] or {}).get("urgency") == "high"
            if urgent and rule.trigger != "before_appointment":
                return "긴급 문의를 처리 중이에요. 문의함에서 먼저 확인해 주세요."
            if rule.is_ad:
                return "답하지 않은 문의가 있어요. 문의함에서 먼저 답해 주세요."
        if rule.is_ad and already_today:
            return "오늘 이미 다른 안내가 나가요. 광고성 메시지는 하루 1건까지만 보내요."
        return None

    @staticmethod
    def _overlap_flags(rule: RecallRule, thread: dict | None) -> list[dict]:
        """처리 결과 안내가 문의함 답장 초안에도 있으면 알려 준다. 답장을 못 보낼 수도 있으니 막지는 않는다."""
        if rule.trigger != "consent_changed" or not thread or not mentions_result_notice((thread["analysis"] or {}).get("draft_reply", "")):
            return []
        return [{"phrase": "문의함 답장과 겹침", "kind": "review", "blocking": False,
                 "reason": "문의함 답장 초안에도 같은 안내가 있어요. 답장을 먼저 보내면 이 알림톡은 저절로 빠지고, 답장을 못 보내면 이 알림톡을 보내 주세요."}]

    def _recheck(self, item: dict, rule: RecallRule, day: str | None) -> str | None:
        """승인·발송 직전에 다시 본다: 그사이 들어온 수신거부·채널 차단·새 문의, 문의함 답장으로 이미 알린 처리 결과.

        day를 주면 광고 '하루 1건'도 그날 기준으로 다시 센다.
        """
        customer = self.crm.get(item["customer_id"])
        if customer is None:
            return "고객 정보를 찾을 수 없어요."
        if rule.trigger == "consent_changed" and self._notified(customer.id, item["anchor"].split(":", 1)[1].rsplit(":", 1)[0]):
            return "문의함 답장으로 이미 알렸어요."
        channel, _, route_problem = delivery.route(rule, customer)
        if not route_problem and channel != item["channel"]:
            # 예: 채널을 차단해 친구가 아니게 됐다(브랜드 메시지의 수신거부). 문자로 몰래 바꾸지 않고 실장이 다시 판단한다.
            route_problem = "승인 뒤 보낼 경로가 바뀌었어요(채널 차단 등). 다시 확인해 주세요."
        already = bool(day) and item["customer_id"] in self.store.customers_sending_on(day, exclude_id=item["id"])
        return self._block_reason(rule, customer, route_problem, self._open_threads().get(customer.id), already)

    def _notified(self, customer_id: str, since: str) -> bool:
        """그 뒤에 보낸 문의함 답장에 처리 결과 안내가 들어 있었는지."""
        return any(log.at >= since and mentions_result_notice(log.reply) for log in self.crm.contact_logs(customer_id))

    def _values(self, customer: Customer, hit_values: dict, target: date, channel: str) -> dict:
        return {
            **hit_values,
            "이름": customer.name,
            "병원": self.clinic_name,
            "대표번호": self.clinic_phone,
            "다음예약안내": next_visit_sentence(customer, target),
            "수신거부안내": CHANNELS[channel].opt_out or "",
            "무료수신거부": CHANNELS["sms"].opt_out,
        }

    def _flags(self, rule: RecallRule, channel: str, text: str) -> list[dict]:
        flags = compliance.check(text)
        if rule.is_ad:
            flags += compliance.check_ad_format(
                text, self.clinic_name, CHANNELS[channel].opt_out,
                contact=self.clinic_phone, subject=delivery.subject(channel, True, self.clinic_name),
            )
        flags += delivery.length_flags(channel, text)
        flags += [{"phrase": "{" + v + "}", "reason": "채우지 못한 템플릿 변수예요.", "blocking": True} for v in unfilled(text)]
        return flags

    def frame_ad(self, body: str, channel: str) -> str:
        """법정 표시는 모델에 맡기지 않고 코드가 정해진 문구로 붙인다.

        로컬 모델에게 수신거부 문구를 그대로 옮기라고 했더니 '무료' 두 글자를 빠뜨렸다(실제 실행).
        모델이 머리줄·수신거부 줄을 써 왔으면 떼어 내고 다시 붙인다.
        """
        lines = [line.rstrip() for line in (body or "").strip().splitlines()]
        while lines and (lines[0].lstrip().startswith("(광고)") or not lines[0].strip()):
            lines.pop(0)
        while lines and ("수신거부" in lines[-1] or re.fullmatch(r"\s*대표번호\s*[\d-]+\s*", lines[-1]) or not lines[-1].strip()):
            lines.pop()
        header = f"(광고) {self.clinic_name} 대표번호 {self.clinic_phone}"
        return f"{header}\n\n" + "\n".join(lines) + f"\n\n{CHANNELS[channel].opt_out}"

    def check(self, item_id: int, text: str) -> list[dict]:
        """화면에서 고쳐 쓰는 동안의 실시간 점검. 승인할 때와 같은 규칙을 쓴다(AI 초안 검토 경고는 화면이 따로 붙인다)."""
        item = self.store.get(item_id)
        return self._flags(self.rules_by_id[item["rule_id"]], item["channel"], (text or "").strip())

    # ---- 개인화 메시지 -------------------------------------------------------

    def build_prompt(self, item: dict, rule: RecallRule, customer: Customer, masker: Masker) -> str:
        token = masker.token_for("고객", customer.name)
        channel = CHANNELS[item["channel"]]
        lines = [
            f"[기준 날짜] {item['date']}",
            f"[발송 구분] {rule.kind}" + (" (정보통신망법상 광고성 정보)" if rule.is_ad else ""),
            f"[발송 채널] {channel.label}",
        ]
        if rule.is_ad:
            lines.append("[광고 표시] '(광고)' 머리줄과 무료 수신거부 안내는 시스템이 붙입니다. 본문만 쓰세요.")
        lines += [
            f"[발송 목적] {rule.name}: {rule.purpose}",
            f"[발송 이유] {item['reason']}",
            "",
            "[CRM 고객 정보]",
            f"- 고객: {token} · 선호 언어 {customer.language} · 태그: {', '.join(customer.tags) or '없음'}",
        ]
        lines += [f"- 상담 이력: {c.date} {c.procedure} · {c.counselor} · {c.status}" + (f" · 메모: {c.note}" if c.note else "")
                  for c in customer.consultations]
        lines += [f"- 예약/수술: {a.date} {a.kind} {a.procedure} ({a.status})" for a in customer.appointments]
        lines += [f"- 최근 응대: {log.at[:10]} {log.summary}" for log in self.crm.contact_logs(customer.id)[-2:]]

        procedure = item["anchor"].split(":")[-1]
        chunks = self.knowledge.search(f"{procedure} {rule.purpose}", k=2, category=category_of(procedure))
        if price := self.knowledge.by_section("가격 안내 원칙"):
            chunks.append(price)
        lines += ["", "[병원 안내 자료]"]
        lines += [f'<source id="{c.id}" title="{c.title}">{c.text}</source>' for c in chunks]
        lines += ["", "위 고객에게 보낼 메시지를 작성하세요."]
        return "\n".join(masker.mask(line) for line in lines)

    def draft(self, item_id: int) -> None:
        item = self.store.get(item_id)
        try:
            rule = self.rules_by_id[item["rule_id"]]
            customer = self.crm.get(item["customer_id"])
            target = date.fromisoformat(item["date"])
            masker = Masker([customer.name])
            prompt = self.build_prompt(item, rule, customer, masker)
            hit = next((h for h in find_hits(rule, customer, target) if h.anchor == item["anchor"]), None)
            fallback = fill_template(rule.template, self._values(customer, hit.values if hit else {}, target, item["channel"]))

            text, notes, mode, note = fallback, "", "template", ""
            if self.writer:
                try:
                    data = self.writer.write(prompt)
                    text, notes, mode = masker.unmask(data["message"]), masker.unmask(data["notes"]), self.writer.mode
                    if rule.is_ad:
                        text = self.frame_ad(text, item["channel"])
                except DrafterError as e:
                    note = f"{e} 기본 문구로 대신했어요."
            flags = self._flags(rule, item["channel"], text)
            if mode != "template":
                allowed = {self.clinic_phone, *(c.opt_out for c in CHANNELS.values() if c.opt_out)}
                flags += review.check(text) + review.check_recall(text) + review.unknown_numbers(
                    "\n".join(line for line in text.splitlines() if "수신거부" not in line), allowed
                )
            self.store.update(
                item_id, only_if_status="drafting", status="ready", text=text, notes=notes, flags=flags,
                prompt=prompt, masked=masker.replacements, mode=mode, note=note,
            )
        except Exception as e:  # 한 건이 실패해도 나머지 리콜은 계속 준비돼야 한다
            self.store.update(item_id, only_if_status="drafting", status="error", note=f"메시지를 준비하지 못했어요: {e}")

    # ---- 승인·예약·발송 ------------------------------------------------------

    def _send_at(self, item: dict, rule: RecallRule) -> datetime:
        """예정 시각이 지났으면 지금, 광고가 허용 시간 밖이면 다음 날 낮."""
        return delivery.send_time(item["channel"], rule.is_ad, max(datetime.fromisoformat(item["scheduled_at"]), self._now()))

    def approve(self, item_id: int, text: str | None = None, handled_by: str = "상담실장") -> dict:
        item = self.store.get(item_id)
        if item is None or item["status"] != "ready":
            raise ValueError("보낼 수 있는 상태가 아니에요.")
        rule = self.rules_by_id[item["rule_id"]]
        channel = item["channel"]
        # 알림톡은 검수된 문구만 보낼 수 있어 수정본을 받지 않는다.
        final = item["text"] if rule.delivery == "template" or not text else text.strip()
        flags = self._flags(rule, channel, final)
        if blocking := [f for f in flags if f.get("blocking")]:
            raise ValueError("고쳐야 보낼 수 있어요: " + ", ".join(f["phrase"] for f in blocking))

        send_at = self._send_at(item, rule)
        if rule.is_ad and item["customer_id"] in self.store.customers_sending_on(send_at.date().isoformat(), exclude_id=item_id):
            raise ValueError(f"{send_at.month}/{send_at.day}에 이미 다른 안내가 나가요. 광고성 메시지는 하루 1건까지만 보내요.")

        # 한 번만 집는다. 그 뒤로 들어오는 수신거부는 아래 재확인이 잡는다(집은 건은 리스너가 건드리지 않는다).
        if not self.store.update(item_id, only_if_status="ready", status="sending"):
            raise ValueError("그사이 상태가 바뀌었어요(수신거부 반영 등). 다시 확인해 주세요.")
        if reason := self._recheck(item, rule, send_at.date().isoformat() if rule.is_ad else None):
            self.store.update(item_id, status="blocked", block_reason=f"승인 직전 재확인: {reason}")
            raise ValueError(f"보내지 않았어요: {reason}")
        if send_at > self._now():
            self.store.update(
                item_id, status="scheduled", text=final, flags=flags, scheduled_at=_minutes(send_at), approved_by=handled_by,
            )
        else:
            self._send({**item, "text": final, "approved_by": handled_by}, rule, send_at)
        return self.store.get(item_id)

    def _send(self, item: dict, rule: RecallRule, send_at: datetime) -> None:
        channel = item["channel"]
        sent_at = _minutes(send_at)
        self.channels.send(
            channel, item["recipient"], item["text"], send_at=sent_at,
            is_ad=rule.is_ad, subject=delivery.subject(channel, rule.is_ad, self.clinic_name),
        )
        self.crm.log_contact(
            item["customer_id"],
            ContactLog(
                at=sent_at,
                channel=CHANNELS[channel].label,
                summary=f"[리콜] {rule.name} · {item['reason']}",
                reply=item["text"],
                handled_by=item.get("approved_by") or "상담실장",
            ),
        )
        self.store.update(
            item["id"], status="sent", text=item["text"], sent_at=sent_at, scheduled_at=sent_at,
            flags=self._flags(rule, channel, item["text"]),
        )

    def dispatch_due(self) -> int:
        """예약 시각이 된 메시지를 보낸다. 보내기 직전에 수신 동의·채널·답하지 않은 문의를 다시 확인한다.

        실제 운영에서는 스케줄러가 돌리고, 데모에서는 리콜 화면을 열 때마다 돌린다.
        """
        now = self._now()
        due = self.store.scheduled_due(_minutes(now))
        if not due:
            return 0
        sent = 0
        for item in due:
            rule = self.rules_by_id[item["rule_id"]]
            # 화면 두 개가 동시에 열어도 한 번만 보내도록 먼저 집는다
            if not self.store.update(item["id"], only_if_status="scheduled", status="sending"):
                continue
            reason = self._recheck(item, rule, item["scheduled_at"][:10])
            if reason:
                self.store.update(item["id"], status="blocked", block_reason=f"발송 직전 재확인: {reason}")
                continue
            self._send(item, rule, datetime.fromisoformat(item["scheduled_at"]))
            sent += 1
        return sent

    def on_consent_change(self, customer_id: str, event: ConsentEvent) -> int:
        """수신거부가 CRM에 들어오면 그 고객에게 준비·예약 중이던 메시지 중 해당하는 것을 바로 멈춘다.

        광고 수신 동의 철회 → 광고 전부와 2년 확인 안내. 카카오 채널 차단 → 브랜드 메시지(채널 차단은 광고 수신 동의 철회도
        함께 들어온다). 이미 집어서 보내는 중인 건은 승인·발송 직전 재확인이 맡는다. 다시 동의해도 멈춘 것은 되살리지 않는다.
        """
        if event.value:
            return 0
        stopped = 0
        when = self._when(datetime.fromisoformat(event.at))
        for item in self.store.pending_for(customer_id):
            rule = self.rules_by_id[item["rule_id"]]
            refused = event.field == "marketing_consent" and (rule.is_ad or rule.trigger == "consent_anniversary")
            if refused or (event.field == "kakao_friend" and item["channel"] == "friendtalk"):
                stopped += self.store.update(item["id"], only_if_status=item["status"], status="blocked",
                                             block_reason=f"수신거부 반영: {event.source} ({when})")
        return stopped

    def cancel(self, item_id: int) -> None:
        item = self.store.get(item_id)
        if item is None or item["status"] != "scheduled":
            raise ValueError("예약된 메시지만 취소할 수 있어요.")
        self.store.update(item_id, status="skipped", note="예약 취소")

    def approve_all_info(self, day: str, handled_by: str = "상담실장") -> int:
        count = 0
        for item in self.store.items_for(day):
            rule = self.rules_by_id[item["rule_id"]]
            if item["status"] == "ready" and rule.delivery == "template" and not rule.is_ad and not item["flags"]:
                self.approve(item["id"], handled_by=handled_by)
                count += 1
        return count

    def skip(self, item_id: int, reason: str) -> None:
        item = self.store.get(item_id)
        if item is None or item["status"] not in ("ready", "error", "scheduled"):
            raise ValueError("이미 보냈거나 보류된 메시지는 건너뛸 수 없어요.")
        self.store.update(item_id, status="skipped", note=reason or "실장이 건너뜀")

    def regenerate(self, item_id: int) -> None:
        item = self.store.get(item_id)
        if item and item["status"] in ("ready", "error") and self.rules_by_id[item["rule_id"]].delivery == "ai":
            self.store.update(item_id, status="drafting")
            self.draft(item_id)

    # ---- 화면용 -------------------------------------------------------------

    def _when(self, moment: datetime) -> str:
        return f"{moment:%H:%M}" if moment.date() == self._now().date() else f"{moment.month}/{moment.day} {moment:%H:%M}"

    def _decorate(self, item: dict) -> dict:
        rule = self.rules_by_id[item["rule_id"]]
        channel = CHANNELS.get(item["channel"])
        extra = {"send_label": "", "send_preview": "", "send_now": False}
        if item["status"] == "sent":
            extra["send_label"] = f"발송 완료 · {self._when(datetime.fromisoformat(item['sent_at']))}"
        elif item["status"] == "scheduled":
            extra["send_label"] = f"발송 예약 · {self._when(datetime.fromisoformat(item['scheduled_at']))}"
        elif item["status"] == "ready" and channel:
            preview = self._send_at(item, rule)
            extra.update(send_preview=_minutes(preview), send_now=preview <= self._now(), send_label=self._when(preview))
        return {
            **item,
            **extra,
            "rule_name": rule.name,
            "kind": rule.kind,
            "delivery": rule.delivery,
            "channel_label": channel.label if channel else "보낼 경로 없음",
            "recipient_display": mask_recipient(item["recipient"] or ""),
        }

    def items(self, day: str) -> list[dict]:
        self.dispatch_due()
        return [self._decorate(i) for i in self.store.items_for(day)]

    def item_view(self, item_id: int) -> dict | None:
        self.dispatch_due()
        item = self.store.get(item_id)
        if item is None:
            return None
        customer = self.crm.get(item["customer_id"])
        return {
            **self._decorate(item),
            "customer": asdict(customer) if customer else None,
            "contact_logs": [asdict(log) for log in self.crm.contact_logs(item["customer_id"])],
            "consent_history": [asdict(e) for e in self.crm.consent_history(item["customer_id"])],
        }

    def rules_view(self, day: str) -> list[dict]:
        items = self.store.items_for(day)
        return [
            {**asdict(r), "count": sum(1 for i in items if i["rule_id"] == r.id and i["status"] != "blocked")}
            for r in sorted(self.rules, key=lambda r: list(self.rules_by_id).index(r.id))
        ]
