"""API가 없는 CRM용 연결부: CRM에서 내보낸 CSV를 읽고, 바뀐 내용은 '가져오기용' CSV로 쌓는다.

병원 직원은 하루 한 번 import_queue/ 폴더의 파일을 CRM의 일괄 가져오기 기능으로 올리면 된다.
엑셀에서 한글이 깨지지 않도록 UTF-8 BOM으로 쓴다.
웹훅 전용 앱처럼 다른 프로세스가 같은 폴더에 쓸 수 있어서, 읽기 전에 파일이 바뀌었으면 다시 읽는다.
"""
from __future__ import annotations

import csv
import functools
import shutil
import threading
from dataclasses import replace
from pathlib import Path

from ..models import CONSENT_FIELDS, Appointment, ConsentEvent, Consultation, ContactLog, Customer, normalize_phone
from .base import CRMAdapter

CUSTOMER_FIELDS = ["id", "name", "phone", "email", "language", "tags", "channel_ids", "marketing_consent", "kakao_friend"]
CONSULT_FIELDS = ["customer_id", "date", "procedure", "counselor", "status", "note"]
APPT_FIELDS = ["customer_id", "date", "kind", "procedure", "status", "time"]
LINK_FIELDS = ["customer_id", "channel", "channel_user_id"]
LOG_FIELDS = ["customer_id", "at", "channel", "summary", "reply", "handled_by"]
CONSENT_EVENT_FIELDS = ["customer_id", "at", "field", "value", "source", "note"]


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _write(path: Path, fields: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _append(path: Path, fields: list[str], row: dict) -> None:
    new = not path.exists()
    with path.open("a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if new:
            writer.writeheader()
        writer.writerow(row)


def _yes(value: str | None) -> bool:
    return (value or "N").strip().upper() in ("Y", "1", "TRUE")


def _customer_row(c: Customer) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "phone": c.phone,
        "email": c.email,
        "language": c.language,
        "tags": "|".join(c.tags),
        "channel_ids": "|".join(f"{ch}:{uid}" for ch, uid in c.channel_ids.items()),
        "marketing_consent": "Y" if c.marketing_consent else "N",
        "kakao_friend": "Y" if c.kakao_friend else "N",
    }


def _customer_from_row(row: dict) -> Customer:
    links = dict(pair.split(":", 1) for pair in row["channel_ids"].split("|") if ":" in pair)
    return Customer(
        id=row["id"],
        name=row["name"],
        phone=row["phone"],
        email=row["email"],
        language=row["language"] or "ko",
        channel_ids=links,
        tags=[t for t in row["tags"].split("|") if t],
        marketing_consent=_yes(row.get("marketing_consent")),
        kakao_friend=_yes(row.get("kakao_friend")),
    )


def _locked(method):
    """읽고-바꾸고-파일에 붙이는 사이에 다른 스레드의 다시 읽기가 끼지 않게 한다."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class CsvCRM(CRMAdapter):
    name = "CSV 내보내기·가져오기 연동"
    description = "API가 없는 CRM에서 내보낸 CSV를 읽고, 변경 사항은 가져오기용 CSV로 쌓아요."

    def __init__(self, directory: Path):
        self.export_dir = directory / "export"
        self.queue_dir = directory / "import_queue"
        self.export_dir.mkdir(parents=True, exist_ok=True)
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._load()

    def _files(self) -> list[Path]:
        return [*sorted(self.export_dir.glob("*.csv")), *sorted(self.queue_dir.glob("*.csv"))]

    def _stamp(self) -> tuple:
        return tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in self._files() if p.exists())

    def _fresh(self) -> None:
        with self._lock:
            if self._stamp() != self._loaded:
                self._load()

    def _load(self) -> None:
        """새 dict에 다 읽은 뒤 한 번에 바꿔 끼운다. 초안을 쓰는 다른 스레드가 반쯤 읽은 고객을 보지 않게 한다."""
        with self._lock:
            stamp = self._stamp()  # 읽기 전에 찍어 둔다. 읽는 사이에 바뀌면 다음 읽기에서 다시 읽는다
            customers: dict[str, Customer] = {}
            for row in _read(self.export_dir / "customers.csv") + _read(self.queue_dir / "new_leads.csv"):
                customer = _customer_from_row(row)
                customers[customer.id] = customer
            for row in _read(self.export_dir / "consultations.csv"):
                if c := customers.get(row.pop("customer_id")):
                    c.consultations.append(Consultation(**row))
            for row in _read(self.export_dir / "appointments.csv"):
                if c := customers.get(row.pop("customer_id")):
                    c.appointments.append(Appointment(**{**row, "time": row.get("time") or ""}))  # 시각 칸이 없던 내보내기도 읽는다
            for row in _read(self.queue_dir / "channel_links.csv"):
                if c := customers.get(row["customer_id"]):
                    c.channel_ids[row["channel"]] = row["channel_user_id"]
            logs: dict[str, list[ContactLog]] = {}
            for row in _read(self.queue_dir / "contact_logs.csv"):
                logs.setdefault(row.pop("customer_id"), []).append(ContactLog(**row))
            # 동의 이력은 CRM에서 내보낸 것 + 이 앱이 쌓은 것. 현재 값은 고객 파일(CRM)을 따르고,
            # 아직 CRM에 가져가지 않은 이 앱의 변경(가져오기 대기열)만 시각 순서대로 덧씌운다. SQLite 연결부와 같은 규칙이다.
            consents: dict[str, list[ConsentEvent]] = {}
            for row in _read(self.export_dir / "consent_events.csv"):
                cid = row.pop("customer_id")
                consents.setdefault(cid, []).append(ConsentEvent(**{**row, "value": _yes(row["value"])}))
            queued = []
            for row in _read(self.queue_dir / "consent_events.csv"):
                cid = row.pop("customer_id")
                queued.append((cid, ConsentEvent(**{**row, "value": _yes(row["value"])})))
            for cid, event in sorted(queued, key=lambda pair: pair[1].at):
                consents.setdefault(cid, []).append(event)
                if (c := customers.get(cid)) is not None:
                    setattr(c, event.field, event.value)
            self._customers, self._logs, self._consents, self._loaded = customers, logs, consents, stamp

    def get(self, customer_id: str) -> Customer | None:
        self._fresh()
        c = self._customers.get(customer_id)
        return replace(c, channel_ids=dict(c.channel_ids)) if c else None

    def all_customers(self) -> list[Customer]:
        self._fresh()
        return [self.get(cid) for cid in sorted(self._customers)]

    def find_by_channel(self, channel: str, channel_user_id: str) -> Customer | None:
        self._fresh()
        found = next((c for c in self._customers.values() if c.channel_ids.get(channel) == channel_user_id), None)
        return self.get(found.id) if found else None

    def find_by_phone(self, phone: str) -> Customer | None:
        self._fresh()
        norm = normalize_phone(phone)
        found = next((c for c in self._customers.values() if norm and normalize_phone(c.phone) == norm), None)
        return self.get(found.id) if found else None

    @_locked
    def create_lead(self, *, name: str, phone: str, email: str, language: str, channel: str, channel_user_id: str) -> Customer:
        self._fresh()
        count = sum(1 for cid in self._customers if cid.startswith("L"))
        customer = Customer(
            id=f"L{count + 1:03d}", name=name, phone=phone, email=email, language=language,
            channel_ids={channel: channel_user_id}, tags=["신규 문의"],
        )
        self._customers[customer.id] = customer
        _append(self.queue_dir / "new_leads.csv", CUSTOMER_FIELDS, _customer_row(customer))
        return self.get(customer.id)

    @_locked
    def link_channel(self, customer_id: str, channel: str, channel_user_id: str) -> None:
        self._fresh()
        self._customers[customer_id].channel_ids[channel] = channel_user_id
        _append(self.queue_dir / "channel_links.csv", LINK_FIELDS,
                {"customer_id": customer_id, "channel": channel, "channel_user_id": channel_user_id})

    @_locked
    def log_contact(self, customer_id: str, log: ContactLog) -> None:
        self._logs.setdefault(customer_id, []).append(log)
        _append(self.queue_dir / "contact_logs.csv", LOG_FIELDS, {"customer_id": customer_id, **log.__dict__})

    def contact_logs(self, customer_id: str) -> list[ContactLog]:
        self._fresh()
        return list(self._logs.get(customer_id, []))

    @_locked
    def update_consent(self, customer_id: str, event: ConsentEvent) -> None:
        if event.field not in CONSENT_FIELDS:
            raise ValueError(f"바꿀 수 없는 항목이에요: {event.field}")
        self._fresh()
        setattr(self._customers[customer_id], event.field, event.value)
        self._consents.setdefault(customer_id, []).append(event)
        _append(self.queue_dir / "consent_events.csv", CONSENT_EVENT_FIELDS,
                {"customer_id": customer_id, **event.__dict__, "value": "Y" if event.value else "N"})

    def consent_history(self, customer_id: str) -> list[ConsentEvent]:
        self._fresh()
        return sorted(self._consents.get(customer_id, []), key=lambda e: e.at)

    @_locked
    def reset(self, customers: list[Customer], consent_events: dict[str, list[ConsentEvent]] | None = None) -> None:
        shutil.rmtree(self.queue_dir, ignore_errors=True)
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        _write(self.export_dir / "customers.csv", CUSTOMER_FIELDS, [_customer_row(c) for c in customers])
        _write(self.export_dir / "consultations.csv", CONSULT_FIELDS,
               [{"customer_id": c.id, **x.__dict__} for c in customers for x in c.consultations])
        _write(self.export_dir / "appointments.csv", APPT_FIELDS,
               [{"customer_id": c.id, **x.__dict__} for c in customers for x in c.appointments])
        _write(self.export_dir / "consent_events.csv", CONSENT_EVENT_FIELDS,
               [{"customer_id": cid, **e.__dict__, "value": "Y" if e.value else "N"}
                for cid, events in (consent_events or {}).items() for e in events])
        self._load()
