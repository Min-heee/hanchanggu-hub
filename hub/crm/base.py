"""병원 CRM과 데이터를 주고받는 연결부.

AI·문의함 로직은 이 인터페이스만 안다. 병원이 쓰는 CRM이 바뀌면 이 클래스를 하나 더 구현하면 된다.
(API가 있으면 API 호출, 없으면 CSV 내보내기·가져오기, 그것도 안 되면 화면 자동화 순서로 검토한다.)
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import ConsentEvent, ContactLog, Customer, normalize_phone


class CRMAdapter(ABC):
    name: str
    description: str

    @abstractmethod
    def get(self, customer_id: str) -> Customer | None: ...

    @abstractmethod
    def all_customers(self) -> list[Customer]:
        """리콜 대상을 찾을 때 쓴다. 실제 CRM이 크면 '최근 N일 내 수술·상담 고객'처럼 좁혀서 가져오면 된다."""

    @abstractmethod
    def find_by_channel(self, channel: str, channel_user_id: str) -> Customer | None: ...

    @abstractmethod
    def find_by_phone(self, phone: str) -> Customer | None: ...

    def find_all_by_phone(self, phone: str) -> list[Customer]:
        """같은 번호로 등록된 고객 전부. 080 수신거부는 번호 단위라, 가족이 번호를 같이 쓰거나 중복 등록돼도 모두 막아야 한다."""
        norm = normalize_phone(phone)
        return [c for c in self.all_customers() if norm and normalize_phone(c.phone) == norm]

    @abstractmethod
    def create_lead(self, *, name: str, phone: str, email: str, language: str, channel: str, channel_user_id: str) -> Customer:
        """CRM에 없던 문의자를 신규 고객(리드)으로 등록한다."""

    @abstractmethod
    def link_channel(self, customer_id: str, channel: str, channel_user_id: str) -> None:
        """다른 채널 계정을 같은 고객으로 묶는다 (예: 인스타 DM 계정 ↔ 카카오 계정)."""

    @abstractmethod
    def log_contact(self, customer_id: str, log: ContactLog) -> None:
        """발송한 답장과 요약을 상담 기록으로 남긴다."""

    @abstractmethod
    def contact_logs(self, customer_id: str) -> list[ContactLog]: ...

    @abstractmethod
    def update_consent(self, customer_id: str, event: ConsentEvent) -> None:
        """광고 수신 동의·카카오 채널 친구 여부를 바꾸고 이력을 남긴다 (080 수신거부, 채널 차단 등)."""

    @abstractmethod
    def consent_history(self, customer_id: str) -> list[ConsentEvent]: ...

    @abstractmethod
    def reset(self, customers: list[Customer], consent_events: dict[str, list[ConsentEvent]] | None = None) -> None:
        """데모용: CRM을 시드 데이터로 되돌린다."""
