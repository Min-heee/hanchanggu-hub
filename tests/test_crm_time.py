"""CRM 예약의 시작 시각과, 시드 예약이 진료하는 날·시간에 놓이는지."""
from datetime import date, timedelta

import pytest

from hub.app import build_hub
from hub.clinic_hours import DOWS, clinic_hours, minutes_of
from hub.crm import CsvCRM
from tests.conftest import TODAY


def test_crm_appointments_keep_their_time(hub):
    appt = next(a for a in hub.crm.get("C002").appointments if a.procedure == "실밥 제거")
    assert (appt.date, appt.time) == ("2026-10-22", "14:00")


@pytest.mark.parametrize("offset", range(7))
def test_seed_visits_land_on_open_days(tmp_path, offset):
    today = TODAY + timedelta(days=offset)
    hub = build_hub(runtime=tmp_path, llm="offline", today=today)
    hub.reset(wait_for_drafts=False)
    hours = clinic_hours()
    for customer in hub.crm.all_customers():
        for a in customer.appointments:
            if a.status != "예정" or not a.time:
                continue
            weekly = hours["weekly"][DOWS[date.fromisoformat(a.date).weekday()]]
            assert weekly, (customer.id, a.date)  # 일요일이 아니다
            assert minutes_of(a.time) <= minutes_of(weekly["close"]) - hours["lastEntryMinutesBeforeClose"], (customer.id, a.date, a.time)


def test_csv_export_without_time_column_still_loads(tmp_path):
    export = tmp_path / "export"
    export.mkdir(parents=True)
    (export / "customers.csv").write_text(
        "id,name,phone,email,language,tags,channel_ids,marketing_consent,kakao_friend\nC1,가,010,,ko,,,N,N\n", encoding="utf-8-sig")
    (export / "appointments.csv").write_text("customer_id,date,kind,procedure,status\nC1,2026-10-22,경과,실밥 제거,예정\n", encoding="utf-8-sig")
    appt = CsvCRM(tmp_path).get("C1").appointments[0]
    assert (appt.procedure, appt.time) == ("실밥 제거", "")
