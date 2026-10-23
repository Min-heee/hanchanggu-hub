"""문의함 자체의 상태(스레드, 메시지, AI 분석 결과). CRM과는 별개로 이 앱이 가진다."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from .models import Analysis, InboundMessage

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY, thread_key TEXT, direction TEXT, channel TEXT, channel_user_id TEXT,
    sender_name TEXT, phone TEXT, email TEXT, text TEXT, at TEXT
);
CREATE TABLE IF NOT EXISTS threads (
    key TEXT PRIMARY KEY, customer_id TEXT, display_name TEXT, status TEXT, last_at TEXT,
    analysis TEXT, flags TEXT, prompt TEXT, masked INTEGER, mode TEXT, note TEXT,
    final_text TEXT, edit_ratio REAL
);
"""

OPEN_STATUSES = ("new", "analyzing", "drafted", "error")

# 그 스레드에 마지막 답장보다 늦게 들어온 고객 메시지가 있는가
_UNANSWERED = (
    "EXISTS (SELECT 1 FROM messages i WHERE i.thread_key = threads.key AND i.direction = 'in' AND i.rowid > "
    "COALESCE((SELECT MAX(o.rowid) FROM messages o WHERE o.thread_key = threads.key AND o.direction = 'out'), 0))"
)


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._db()) as db:
            db.executescript(_SCHEMA)

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        with closing(self._db()) as db:
            return [dict(r) for r in db.execute(sql, params).fetchall()]

    def _write(self, sql: str, params: tuple = ()) -> None:
        with closing(self._db()) as db, db:
            db.execute(sql, params)

    def reset(self) -> None:
        with closing(self._db()) as db, db:
            db.execute("DELETE FROM messages")
            db.execute("DELETE FROM threads")

    def known_message_ids(self) -> set[str]:
        return {r["id"] for r in self._query("SELECT id FROM messages WHERE direction = 'in'")}

    def thread_key_for_customer(self, customer_id: str) -> str | None:
        rows = self._query("SELECT key FROM threads WHERE customer_id = ?", (customer_id,))
        return rows[0]["key"] if rows else None

    def add_inbound(self, key: str, msg: InboundMessage, customer_id: str | None, display_name: str) -> None:
        with closing(self._db()) as db, db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO messages VALUES (?, ?, 'in', ?, ?, ?, ?, ?, ?, ?)",
                (msg.id, key, msg.channel, msg.channel_user_id, msg.sender_name, msg.phone, msg.email, msg.text, msg.received_at),
            ).rowcount
            if not inserted:  # 이미 받은 메시지(웹훅 재전송 등). 답장한 스레드를 다시 열지 않는다
                return
            existing = db.execute("SELECT last_at FROM threads WHERE key = ?", (key,)).fetchone()
            if existing is None:
                db.execute(
                    "INSERT INTO threads (key, customer_id, display_name, status, last_at) VALUES (?, ?, ?, 'new', ?)",
                    (key, customer_id, display_name, msg.received_at),
                )
            else:
                # 처리가 끝난 스레드라도 새 문의가 오면 다시 '처리 필요'로 돌린다.
                db.execute(
                    "UPDATE threads SET status = 'new', last_at = MAX(last_at, ?) WHERE key = ?",
                    (msg.received_at, key),
                )

    def add_outbound(self, key: str, message_id: str, channel: str, channel_user_id: str, text: str, at: str) -> None:
        self._write(
            "INSERT INTO messages VALUES (?, ?, 'out', ?, ?, '상담팀', '', '', ?, ?)",
            (message_id, key, channel, channel_user_id, text, at),
        )

    def set_status(self, key: str, status: str, note: str | None = None) -> None:
        if note is None:
            self._write("UPDATE threads SET status = ? WHERE key = ?", (status, key))
        else:
            self._write("UPDATE threads SET status = ?, note = ? WHERE key = ?", (status, note, key))

    def save_analysis(self, key: str, analysis: Analysis, flags: list[dict], prompt: str, masked: int, mode: str, note: str) -> None:
        self._write(
            # 마지막 답장 뒤에 들어온 문의가 있을 때만 '초안 준비'로 바꾼다. 초안을 쓰는 사이 채널 관리 화면에서 직접 답했으면
            # 답장한 상태를 지키고, 예전 초안으로 답한 뒤 새 문의가 왔으면 다시 연다. 순서는 시각이 아니라 들어온 차례(rowid)로 본다.
            f"UPDATE threads SET status = CASE WHEN {_UNANSWERED} THEN 'drafted' ELSE status END, "
            f"analysis = ?, flags = ?, prompt = ?, masked = ?, mode = ?, note = CASE WHEN {_UNANSWERED} THEN ? ELSE note END WHERE key = ?",
            (json.dumps(asdict(analysis), ensure_ascii=False), json.dumps(flags, ensure_ascii=False), prompt, masked, mode, note, key),
        )

    def mark_sent(self, key: str, final_text: str, edit_ratio: float, customer_id: str) -> None:
        self._write(
            "UPDATE threads SET status = 'sent', final_text = ?, edit_ratio = ?, customer_id = ? WHERE key = ?",
            (final_text, edit_ratio, customer_id, key),
        )

    def thread(self, key: str) -> dict | None:
        rows = self._query("SELECT * FROM threads WHERE key = ?", (key,))
        if not rows:
            return None
        row = rows[0]
        row["analysis"] = json.loads(row["analysis"]) if row["analysis"] else None
        row["flags"] = json.loads(row["flags"]) if row["flags"] else []
        return row

    def threads(self) -> list[dict]:
        return [self.thread(r["key"]) for r in self._query("SELECT key FROM threads")]

    def messages(self, key: str) -> list[dict]:
        # 들어온 차례(rowid)로 늘어놓는다. 시각은 분 단위이고 채널이 주는 시각은 늦게 도착할 수 있어서,
        # '마지막 답장 뒤에 온 문의'를 시각으로 가리면 같은 분이나 지연 도착 때 순서가 뒤집힌다.
        return self._query("SELECT * FROM messages WHERE thread_key = ? ORDER BY rowid", (key,))

    def stuck(self) -> list[str]:
        """초안을 만들다 서버가 꺼져 '분석 중'에 멈춘 스레드."""
        return [r["key"] for r in self._query("SELECT key FROM threads WHERE status IN ('new', 'analyzing')")]

    def metrics(self) -> dict:
        rows = self._query("SELECT status, analysis, edit_ratio FROM threads")
        sent = [r for r in rows if r["status"] == "sent"]
        ratios = [r["edit_ratio"] for r in sent if r["edit_ratio"] is not None]
        open_rows = [r for r in rows if r["status"] in OPEN_STATUSES]
        return {
            "total": len(rows),
            "open": len(open_rows),
            "urgent": sum(1 for r in open_rows if r["analysis"] and json.loads(r["analysis"])["urgency"] == "high"),
            "sent": len(sent),
            "escalated": sum(1 for r in rows if r["status"] == "escalated"),
            "kept_as_is": sum(1 for x in ratios if x >= 0.98),
            "avg_similarity": round(sum(ratios) / len(ratios), 2) if ratios else None,
        }
