"""준비된 리콜 메시지와 발송 기록. 같은 사건(anchor)으로는 한 번만 보낸다."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS recall_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT, customer_id TEXT, customer_name TEXT, rule_id TEXT, anchor TEXT, reason TEXT,
    status TEXT, block_reason TEXT, channel TEXT, recipient TEXT, scheduled_at TEXT,
    text TEXT, notes TEXT, flags TEXT, prompt TEXT, masked INTEGER, mode TEXT, note TEXT,
    sent_at TEXT, approved_by TEXT
);
"""


class RecallStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._db()) as db, db:
            db.executescript(_SCHEMA)
            if "approved_by" not in {r["name"] for r in db.execute("PRAGMA table_info(recall_items)")}:
                db.execute("ALTER TABLE recall_items ADD COLUMN approved_by TEXT")  # 예약 발송이 생기기 전에 만든 DB

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _row(self, row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        item = dict(row)
        item["flags"] = json.loads(item["flags"]) if item["flags"] else []
        return item

    def reset(self) -> None:
        with closing(self._db()) as db, db:
            db.execute("DELETE FROM recall_items")

    def exists(self, customer_id: str, rule_id: str, anchor: str) -> bool:
        """보류(blocked)된 건은 세지 않는다. 동의를 받거나 문의가 끝나면 다음 계획 때 다시 후보가 된다."""
        with closing(self._db()) as db:
            row = db.execute(
                "SELECT 1 FROM recall_items WHERE customer_id = ? AND rule_id = ? AND anchor = ? AND status != 'blocked'",
                (customer_id, rule_id, anchor),
            ).fetchone()
        return row is not None

    def customers_sending_on(self, day: str, exclude_id: int | None = None) -> set[str]:
        """그날 실제로 나가는(이미 보냈거나 예약된) 메시지가 있는 고객. 계획한 날이 아니라 보내는 날 기준으로 센다."""
        with closing(self._db()) as db:
            rows = db.execute(
                "SELECT DISTINCT customer_id FROM recall_items WHERE status IN ('sent', 'scheduled') "
                "AND substr(scheduled_at, 1, 10) = ? AND id != ?",
                (day, exclude_id if exclude_id is not None else -1),
            ).fetchall()
        return {r["customer_id"] for r in rows}

    def scheduled_due(self, until: str) -> list[dict]:
        with closing(self._db()) as db:
            rows = db.execute(
                "SELECT * FROM recall_items WHERE status = 'scheduled' AND scheduled_at <= ? ORDER BY scheduled_at, id", (until,)
            ).fetchall()
        return [self._row(r) for r in rows]

    def clear_blocked(self, day: str) -> None:
        with closing(self._db()) as db, db:
            db.execute("DELETE FROM recall_items WHERE date = ? AND status = 'blocked'", (day,))

    def insert(self, item: dict) -> int:
        fields = {**item, "flags": json.dumps(item.get("flags", []), ensure_ascii=False)}
        columns = ", ".join(fields)
        with closing(self._db()) as db, db:
            cur = db.execute(f"INSERT INTO recall_items ({columns}) VALUES ({', '.join('?' * len(fields))})", tuple(fields.values()))
            return cur.lastrowid

    def update(self, item_id: int, only_if_status: str | None = None, **fields) -> int:
        """바꾼 행 수를 돌려준다. only_if_status: 그 상태일 때만 바꾼다(작성 중 수신거부로 멈춘 건을 되살리지 않기,
        발송할 건을 한 번만 집기)."""
        if "flags" in fields:
            fields["flags"] = json.dumps(fields["flags"], ensure_ascii=False)
        assignments = ", ".join(f"{k} = ?" for k in fields)
        where, params = "id = ?", [item_id]
        if only_if_status is not None:
            where, params = "id = ? AND status = ?", [item_id, only_if_status]
        with closing(self._db()) as db, db:
            return db.execute(f"UPDATE recall_items SET {assignments} WHERE {where}", (*fields.values(), *params)).rowcount

    def get(self, item_id: int) -> dict | None:
        with closing(self._db()) as db:
            return self._row(db.execute("SELECT * FROM recall_items WHERE id = ?", (item_id,)).fetchone())

    def pending_for(self, customer_id: str) -> list[dict]:
        """아직 나가지 않은(작성 중·승인 대기·예약·오류) 메시지. 오류 난 건도 다시 쓰면 승인 대기가 되므로 포함한다."""
        with closing(self._db()) as db:
            rows = db.execute(
                "SELECT * FROM recall_items WHERE customer_id = ? AND status IN ('drafting', 'ready', 'scheduled', 'error') ORDER BY id",
                (customer_id,),
            ).fetchall()
        return [self._row(r) for r in rows]

    def unsent_item(self, customer_id: str, rule_id: str, anchor: str) -> dict | None:
        """같은 사건으로 만든, 아직 승인하지 않은 메시지 (법정 기한이 있는 안내를 다음 날로 넘길 때 쓴다)."""
        with closing(self._db()) as db:
            return self._row(db.execute(
                "SELECT * FROM recall_items WHERE customer_id = ? AND rule_id = ? AND anchor = ? AND status IN ('drafting', 'ready', 'error')",
                (customer_id, rule_id, anchor),
            ).fetchone())

    def items_for(self, day: str) -> list[dict]:
        with closing(self._db()) as db:
            return [self._row(r) for r in db.execute("SELECT * FROM recall_items WHERE date = ? ORDER BY id", (day,)).fetchall()]
