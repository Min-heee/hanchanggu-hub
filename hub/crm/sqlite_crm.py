"""API로 붙는 CRM을 흉내 낸 데모 CRM. 실제 병원 CRM에 API가 있으면 이 자리에 그 호출이 들어간다."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from ..models import CONSENT_FIELDS, Appointment, ConsentEvent, Consultation, ContactLog, Customer, normalize_phone
from .base import CRMAdapter

_SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY, name TEXT, phone TEXT, phone_norm TEXT, email TEXT, language TEXT, tags TEXT,
    marketing_consent INTEGER DEFAULT 0, kakao_friend INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS channel_links (
    channel TEXT, channel_user_id TEXT, customer_id TEXT, PRIMARY KEY (channel, channel_user_id)
);
CREATE TABLE IF NOT EXISTS consultations (
    customer_id TEXT, date TEXT, procedure TEXT, counselor TEXT, status TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS appointments (
    customer_id TEXT, date TEXT, kind TEXT, procedure TEXT, status TEXT, time TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS contact_logs (
    customer_id TEXT, at TEXT, channel TEXT, summary TEXT, reply TEXT, handled_by TEXT
);
CREATE TABLE IF NOT EXISTS consent_events (
    customer_id TEXT, at TEXT, field TEXT, value INTEGER, source TEXT, note TEXT
);
"""
# 칸 순서에 기대지 않게 INSERT마다 칸 이름을 적는다 (칸을 ALTER로 덧붙인 예전 DB도 있다).
_CUSTOMER_COLUMNS = "id, name, phone, phone_norm, email, language, tags, marketing_consent, kakao_friend"


class SqliteCRM(CRMAdapter):
    name = "데모 CRM (API 연동형)"
    description = "병원 CRM에 API가 있을 때의 연동 방식을 로컬 DB로 흉내 냈어요."

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._db()) as db, db:
            db.executescript(_SCHEMA)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(customers)")}
            for column in ("marketing_consent", "kakao_friend"):  # 이 칸들이 생기기 전에 만든 DB
                if column not in columns:
                    db.execute(f"ALTER TABLE customers ADD COLUMN {column} INTEGER DEFAULT 0")
            if "time" not in {r["name"] for r in db.execute("PRAGMA table_info(appointments)")}:  # 예약 시각이 생기기 전 DB
                db.execute("ALTER TABLE appointments ADD COLUMN time TEXT DEFAULT ''")

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with closing(self._db()) as db:
            return db.execute(sql, params).fetchall()

    def _write(self, sql: str, params: tuple = ()) -> None:
        with closing(self._db()) as db, db:
            db.execute(sql, params)

    def get(self, customer_id: str) -> Customer | None:
        rows = self._query("SELECT * FROM customers WHERE id = ?", (customer_id,))
        if not rows:
            return None
        row = rows[0]
        links = self._query("SELECT channel, channel_user_id FROM channel_links WHERE customer_id = ?", (customer_id,))
        consults = self._query(
            "SELECT date, procedure, counselor, status, note FROM consultations WHERE customer_id = ? ORDER BY date", (customer_id,)
        )
        appts = self._query(
            "SELECT date, kind, procedure, status, COALESCE(time, '') AS time FROM appointments WHERE customer_id = ? ORDER BY date, time",
            (customer_id,),
        )
        return Customer(
            id=row["id"],
            name=row["name"],
            phone=row["phone"],
            email=row["email"],
            language=row["language"],
            channel_ids={r["channel"]: r["channel_user_id"] for r in links},
            tags=json.loads(row["tags"] or "[]"),
            consultations=[Consultation(**dict(r)) for r in consults],
            appointments=[Appointment(**dict(r)) for r in appts],
            marketing_consent=bool(row["marketing_consent"]),
            kakao_friend=bool(row["kakao_friend"]),
        )

    def all_customers(self) -> list[Customer]:
        return [self.get(r["id"]) for r in self._query("SELECT id FROM customers ORDER BY id")]

    def find_by_channel(self, channel: str, channel_user_id: str) -> Customer | None:
        rows = self._query(
            "SELECT customer_id FROM channel_links WHERE channel = ? AND channel_user_id = ?", (channel, channel_user_id)
        )
        return self.get(rows[0]["customer_id"]) if rows else None

    def find_by_phone(self, phone: str) -> Customer | None:
        norm = normalize_phone(phone)
        rows = self._query("SELECT id FROM customers WHERE phone_norm = ? AND phone_norm != ''", (norm,)) if norm else []
        return self.get(rows[0]["id"]) if rows else None

    def create_lead(self, *, name: str, phone: str, email: str, language: str, channel: str, channel_user_id: str) -> Customer:
        count = self._query("SELECT COUNT(*) AS n FROM customers WHERE id LIKE 'L%'")[0]["n"]
        customer_id = f"L{count + 1:03d}"
        self._write(
            f"INSERT INTO customers ({_CUSTOMER_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)",
            (customer_id, name, phone, normalize_phone(phone), email, language, json.dumps(["신규 문의"], ensure_ascii=False)),
        )
        self.link_channel(customer_id, channel, channel_user_id)
        return self.get(customer_id)

    def link_channel(self, customer_id: str, channel: str, channel_user_id: str) -> None:
        self._write("INSERT OR REPLACE INTO channel_links VALUES (?, ?, ?)", (channel, channel_user_id, customer_id))

    def log_contact(self, customer_id: str, log: ContactLog) -> None:
        self._write(
            "INSERT INTO contact_logs VALUES (?, ?, ?, ?, ?, ?)",
            (customer_id, log.at, log.channel, log.summary, log.reply, log.handled_by),
        )

    def contact_logs(self, customer_id: str) -> list[ContactLog]:
        rows = self._query(
            "SELECT at, channel, summary, reply, handled_by FROM contact_logs WHERE customer_id = ? ORDER BY at", (customer_id,)
        )
        return [ContactLog(**dict(r)) for r in rows]

    def update_consent(self, customer_id: str, event: ConsentEvent) -> None:
        if event.field not in CONSENT_FIELDS:
            raise ValueError(f"바꿀 수 없는 항목이에요: {event.field}")
        with closing(self._db()) as db, db:
            db.execute(f"UPDATE customers SET {event.field} = ? WHERE id = ?", (int(event.value), customer_id))
            db.execute(
                "INSERT INTO consent_events VALUES (?, ?, ?, ?, ?, ?)",
                (customer_id, event.at, event.field, int(event.value), event.source, event.note),
            )

    def consent_history(self, customer_id: str) -> list[ConsentEvent]:
        rows = self._query(
            "SELECT at, field, value, source, note FROM consent_events WHERE customer_id = ? ORDER BY at, rowid", (customer_id,)
        )
        return [ConsentEvent(at=r["at"], field=r["field"], value=bool(r["value"]), source=r["source"], note=r["note"]) for r in rows]

    def reset(self, customers: list[Customer], consent_events: dict[str, list[ConsentEvent]] | None = None) -> None:
        with closing(self._db()) as db, db:
            for table in ("customers", "channel_links", "consultations", "appointments", "contact_logs", "consent_events"):
                db.execute(f"DELETE FROM {table}")
            for cid, events in (consent_events or {}).items():
                db.executemany(
                    "INSERT INTO consent_events VALUES (?, ?, ?, ?, ?, ?)",
                    [(cid, e.at, e.field, int(e.value), e.source, e.note) for e in events],
                )
            for c in customers:
                db.execute(
                    f"INSERT INTO customers ({_CUSTOMER_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (c.id, c.name, c.phone, normalize_phone(c.phone), c.email, c.language,
                     json.dumps(c.tags, ensure_ascii=False), int(c.marketing_consent), int(c.kakao_friend)),
                )
                db.executemany(
                    "INSERT INTO channel_links VALUES (?, ?, ?)", [(ch, uid, c.id) for ch, uid in c.channel_ids.items()]
                )
                db.executemany(
                    "INSERT INTO consultations VALUES (?, ?, ?, ?, ?, ?)",
                    [(c.id, x.date, x.procedure, x.counselor, x.status, x.note) for x in c.consultations],
                )
                db.executemany(
                    "INSERT INTO appointments (customer_id, date, kind, procedure, status, time) VALUES (?, ?, ?, ?, ?, ?)",
                    [(c.id, x.date, x.kind, x.procedure, x.status, x.time) for x in c.appointments],
                )
