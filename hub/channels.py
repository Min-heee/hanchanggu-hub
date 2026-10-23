"""카카오톡·네이버 톡톡·인스타그램 DM·홈페이지 상담 연동 자리.

데모에서는 시드 문의를 배치 단위로 내보내고, 보낸 답장은 outbox.jsonl에 남긴다.
실제 연동은 채널별 웹훅으로 받은 메시지를 InboundMessage로 바꿔 Hub.ingest에 넘기고(지금은 네이버 톡톡),
그 대화에 대한 답장만 ChannelRouter가 실제 API로 보낸다.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .models import InboundMessage, now_iso, now_kst, resolve_relative
from .talktalk import TalkTalkClient


class MockChannels:
    def __init__(self, seed_path: Path, outbox_path: Path, today: date | None = None):
        self.seed_path = seed_path
        self.outbox_path = outbox_path
        self._today = today

    @property
    def today(self) -> date:
        return self._today or now_kst().date()

    def _all(self) -> list[tuple[int, InboundMessage]]:
        data = json.loads(self.seed_path.read_text(encoding="utf-8"))
        result = []
        for raw in data["messages"]:
            fields = {k: v for k, v in raw.items() if k != "batch"}
            fields["received_at"] = resolve_relative(fields["received_at"], self.today)
            result.append((raw.get("batch", 1), InboundMessage(**fields)))
        return result

    def fetch_new(self, known_ids: set[str]) -> list[InboundMessage]:
        pending = [(batch, m) for batch, m in self._all() if m.id not in known_ids]
        if not pending:
            return []
        first = min(batch for batch, _ in pending)
        return sorted((m for batch, m in pending if batch == first), key=lambda m: m.received_at)

    def remaining(self, known_ids: set[str]) -> int:
        return sum(1 for _, m in self._all() if m.id not in known_ids)

    def send(
        self, channel: str, channel_user_id: str, text: str,
        send_at: str | None = None, is_ad: bool = False, subject: str | None = None, tag: str | None = None,
        reply_to: str | None = None, via: str | None = None,
    ) -> dict:
        """보낸 메시지를 outbox에 남긴다.

        send_at: 실제 발송 시각(예약 발송이면 예약한 시각). is_ad: 실제 브랜드 메시지 연동에서는 이 광고 표시를 켜면
        카카오가 '(광고)'와 채널 차단 안내를 붙인다. 광고 문자는 080 번호를 등록한 대행사의 광고 문자 API로 보낸다.
        subject: 문자(LMS) 제목. tag: 인스타 24시간이 지난 사람 답장에 붙이는 HUMAN_AGENT 같은 메시지 태그.
        reply_to: 답하는 문의의 메시지 ID. via: 실제 API로 보냈으면 그 이름(기록용).
        """
        record = {"at": now_iso(), "channel": channel, "to": channel_user_id, "text": text}
        if send_at:
            record["send_at"] = send_at
        if is_ad:
            record["ad"] = True
        if subject:
            record["subject"] = subject
        if tag:
            record["tag"] = tag
        if via:
            record["via"] = via
        self.outbox_path.parent.mkdir(parents=True, exist_ok=True)
        with self.outbox_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def reset(self) -> None:
        self.outbox_path.unlink(missing_ok=True)


class ChannelRouter:
    """데모 채널에 실제 연동을 끼운다. 실제 웹훅으로 들어온 대화(메시지 ID가 talktalk:로 시작)에 대한 답장만
    실제 API로 보내고, 나머지(시드 문의, 리콜 알림톡·문자)는 지금처럼 데모 outbox에 남긴다."""

    def __init__(self, demo: MockChannels, talktalk: TalkTalkClient | None = None):
        self.demo = demo
        self.talktalk = talktalk

    def __getattr__(self, name):  # fetch_new, remaining, reset, outbox_path 등은 데모 채널 것을 쓴다
        return getattr(self.demo, name)

    def send(self, channel: str, channel_user_id: str, text: str, reply_to: str | None = None, **kwargs) -> dict:
        if channel == "naver" and reply_to and reply_to.startswith("talktalk:"):
            if self.talktalk is None:
                raise ValueError("네이버 톡톡 보내기 API 키(NAVER_TALKTALK_AUTH_KEY)가 설정되지 않았어요.")
            self.talktalk.send_text(channel_user_id, text)  # 실패하면 TalkTalkError로 멈추고 '보냄'으로 바꾸지 않는다
            kwargs["via"] = "naver_talktalk_api"
        return self.demo.send(channel, channel_user_id, text, reply_to=reply_to, **kwargs)
