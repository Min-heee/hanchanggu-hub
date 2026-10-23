"""빈자리 탭에 넘기는 바깥 입력(feed.js)과 그 재료(CRM 예약 시각, 데모성형외과 규칙 문서)."""
import json
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from collections import defaultdict
from datetime import timedelta

from hub.app import build_hub, build_recall, create_app
from hub.binjari_feed import build_feed, crm_bookings, feed_js, filler_bookings, service_for
from hub.binjari_rules import CAPACITY, DOWS, FILLER_DAYS, clinic_hours, minutes_of, uses
from hub.crm import CsvCRM
from hub.models import Appointment
from tests.conftest import TODAY

ROOT = Path(__file__).resolve().parent.parent
BINJARI = Path.home() / ".openclaw/workspace/binjari"
NOW = datetime(2026, 10, 21, 9, 30, 45)


def test_crm_appointments_keep_their_time(hub):
    appt = next(a for a in hub.crm.get("C002").appointments if a.procedure == "실밥 제거")
    assert (appt.date, appt.time) == ("2026-10-22", "14:00")


def test_feed_carries_now_and_crm_bookings_without_personal_data(hub):
    feed = build_feed(hub.crm, NOW, "데모성형외과", include_filler=False)
    assert feed["now"] == "2026-10-21T09:30:00+09:00"  # 빈자리는 +09:00이 붙은 초 단위 시각만 받는다
    assert feed["v"] == 1 and feed["clinicName"] == "데모성형외과"
    bookings = feed["bookings"]["bookings"]
    assert [(b["date"], b["start"], b["service"]) for b in bookings] == [
        ("2026-10-22", "14:00", "postop-visit"),
        ("2026-10-26", "15:30", "postop-visit"),  # 시드 '+4일'이 일요일(10/25)이라 다음 진료일로
        ("2026-10-26", "16:00", "postop-visit"),
        ("2026-10-27", "11:30", "postop-visit"),
        ("2026-11-20", "11:00", "postop-visit"),
    ]
    text = json.dumps(feed, ensure_ascii=False)
    for customer in hub.crm.all_customers():
        assert customer.id not in text and customer.name not in text
        assert not customer.phone or customer.phone not in text
    assert feed["sourceLabel"] == "CRM 예약 5건"


def test_untimed_appointments_are_counted_not_guessed(hub):
    customers = hub.crm.all_customers()
    c = next(x for x in customers if x.id == "C002")
    c.appointments.append(Appointment(date="2026-10-24", kind="상담", procedure="재상담", status="예정"))
    hub.crm.reset([c if x.id == "C002" else x for x in customers])
    feed = build_feed(hub.crm, NOW, "데모성형외과", include_filler=False)
    assert feed["sourceLabel"] == "CRM 예약 5건(시각 미정 1건 제외)"  # 시각을 지어내 넣지 않는다


@pytest.mark.parametrize("kind, procedure, service", [
    ("수술", "코성형", "surgery-half"), ("수술", "안면윤곽(광대·사각턱)", "surgery-full"), ("시술", "필러", "petit"),
    ("수술", "광대축소", "surgery-full"), ("수술", "사각턱 수술", "surgery-full"), ("수술", "턱끝성형", "surgery-full"),
    ("상담", "코성형", "first-consult"), ("상담", "코성형 재상담", "revisit-consult"), ("경과", "실밥 제거", "postop-visit"),
])
def test_service_mapping(kind, procedure, service):
    assert service_for(Appointment(date="2026-10-22", kind=kind, procedure=procedure)) == service


def test_demo_filler_is_deterministic_and_respects_closed_sundays():
    first, second = filler_bookings(TODAY), filler_bookings(TODAY)
    assert first == second and len(first) > 50
    assert all(date.fromisoformat(b["date"]).weekday() != 6 for b in first)
    saturday = [b for b in first if date.fromisoformat(b["date"]).weekday() == 5]
    assert saturday and all(b["start"] <= "15:30" for b in saturday)  # 토요일은 16시에 닫는다(접수 30분 전 마감)
    # 수술 첫 가능일 + 수술 검색 기간 + '더 보기' 두 번까지 채워, 뒤쪽 날짜가 갑자기 텅 비어 보이지 않게 한다
    assert max(b["date"] for b in first) >= (TODAY + timedelta(days=FILLER_DAYS - 7)).isoformat()


@pytest.mark.parametrize("offset", range(7))
def test_filler_never_overbooks_people_or_rooms(hub, offset):
    today = TODAY + timedelta(days=offset)
    crm_part, _, _ = crm_bookings(hub.crm, today)
    used = defaultdict(lambda: defaultdict(int))
    hours = clinic_hours()
    for b in crm_part + filler_bookings(today, crm_part):
        for resource, start, end, count in uses(b["service"], b["start"]):
            for minute in range(start, end):
                used[(b["date"], resource)][minute] += count
        if b in crm_part:
            continue
        close = minutes_of(hours["weekly"][DOWS[date.fromisoformat(b["date"]).weekday()]]["close"])
        assert minutes_of(b["start"]) <= close - hours["lastEntryMinutesBeforeClose"]
    over = [(day, resource, m) for (day, resource), per in used.items() for m, n in per.items() if n > CAPACITY[resource]]
    assert over == []


def test_feed_matches_binjari_exact_contract(hub):
    feed = build_feed(hub.crm, NOW, "데모성형외과")
    assert set(feed) == {"v", "now", "clinicName", "sourceLabel", "bookings"}  # 빈자리는 모르는 키가 있으면 멈춘다
    assert set(feed["bookings"]) == {"bookings", "exceptions"}
    ids = [b["id"] for b in feed["bookings"]["bookings"]]
    assert len(ids) == len(set(ids))
    for b in feed["bookings"]["bookings"]:
        assert set(b) == {"id", "service", "date", "start"}
        assert re.fullmatch(r"\d{2}:\d{2}", b["start"]) and date.fromisoformat(b["date"])
        assert re.fullmatch(r"[AF]\d{4}", b["id"])  # 차례 번호만. 고객 ID에서 거꾸로 찾을 수 있는 값이 아니다
    assert sum(i.startswith("A") for i in ids) == 5


def test_messy_crm_times_are_cleaned_or_skipped_not_fatal(tmp_path):
    export = tmp_path / "export"
    export.mkdir(parents=True)
    (export / "customers.csv").write_text(
        "id,name,phone,email,language,tags,channel_ids,marketing_consent,kakao_friend\nC1,가,010,,ko,,,N,N\n", encoding="utf-8-sig")
    (export / "appointments.csv").write_text(
        "customer_id,date,kind,procedure,status,time\n"
        "C1,2026-10-22,경과,실밥 제거,예정,9:30\n"       # 엑셀이 앞자리 0을 지운 값
        "C1,2026-10-23,경과,경과 확인,예정,14:00:00\n"   # 초까지 있는 값
        "C1,2026-10-24,경과,경과 확인,예정,25:00\n"      # 틀린 시각
        "C1,2026/10/26,경과,경과 확인,예정,10:00\n",     # 틀린 날짜
        encoding="utf-8-sig")
    feed = build_feed(CsvCRM(tmp_path), NOW, "데모성형외과", include_filler=False)
    assert [(b["date"], b["start"]) for b in feed["bookings"]["bookings"]] == [("2026-10-22", "09:30"), ("2026-10-23", "14:00")]
    assert feed["sourceLabel"] == "CRM 예약 2건(형식 오류 2건 제외)"


def test_feed_js_cannot_break_out_of_the_script():
    js = feed_js({"v": 1, "clinicName": "</script><script>alert(1)</script>\u2028"})
    assert "</script>" not in js and "\u2028" not in js
    payload = re.match(r"window.__BINJARI_FEED__ = (.*);\n", js).group(1)
    assert json.loads(payload)["clinicName"].startswith("</script>")


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


def test_feed_route_is_fresh_js(hub):
    client = TestClient(create_app(hub, build_recall(hub)))
    assert client.get("/binjari/feed.js", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403  # 다른 사이트가 읽지 못하게
    res = client.get("/binjari/feed.js")
    assert res.status_code == 200 and res.headers["cache-control"] == "no-store"
    assert res.headers["content-type"].startswith("application/javascript")
    body = res.text
    assert body.startswith("window.__BINJARI_FEED__ = {") and 'window.dispatchEvent(new Event("binjari-feed"));' in body
    feed = json.loads(re.match(r"window.__BINJARI_FEED__ = (.*);\n", body).group(1))
    assert feed["sourceLabel"].endswith("+ 데모용 합성 예약")
    assert feed_js(feed).startswith("window.__BINJARI_FEED__")


def test_csv_export_without_time_column_still_loads(tmp_path):
    export = tmp_path / "export"
    export.mkdir(parents=True)
    (export / "customers.csv").write_text(
        "id,name,phone,email,language,tags,channel_ids,marketing_consent,kakao_friend\nC1,가,010,,ko,,,N,N\n", encoding="utf-8-sig")
    (export / "appointments.csv").write_text("customer_id,date,kind,procedure,status\nC1,2026-10-22,경과,실밥 제거,예정\n", encoding="utf-8-sig")
    appt = CsvCRM(tmp_path).get("C1").appointments[0]
    assert (appt.procedure, appt.time) == ("실밥 제거", "")


@pytest.mark.skipif(not (BINJARI / "vault/booking-slots.md").exists(), reason="빈자리 저장소가 이 컴퓨터에 없다")
def test_vault_matches_the_services_the_feed_uses(tmp_path):
    out = tmp_path / "vault"
    subprocess.run([sys.executable, "-I", str(ROOT / "scripts/make_binjari_vault.py"), str(BINJARI), str(out)], check=True,
                   capture_output=True)
    docs = {p.name: p.read_text(encoding="utf-8") for p in out.glob("*.md")}
    ids = {re.search(r"^id: (\S+)$", text, re.M).group(1) for text in docs.values()}
    assert {"V01", "V02", "V03", "V04", "V05", "V06", "V07", "V08", "V09", "B01", "B02", "V15"} <= ids
    for text in docs.values():  # 빈자리 번들 기준일(9/21)보다 늦으면 규칙 화면이 문서를 고르지 못한다
        assert re.search(r"^effective: (\S+)$", text, re.M).group(1) <= "2026-09-21"
    b01 = json.loads(re.search(r"```json\n(.*?)\n```", docs["booking-slots.md"], re.S).group(1))
    used = {b["service"] for b in filler_bookings(TODAY)} | {
        service_for(Appointment(date="", kind=k, procedure=p)) for k, p in [("수술", "코"), ("수술", "윤곽"), ("시술", ""), ("상담", ""), ("경과", "")]}
    assert used <= set(b01["services"])
    assert "데모성형외과" in docs["booking-slots.md"] and "샘플의원" not in docs["booking-slots.md"]
    assert b01["surgeryFollowUps"] == {"nextDayMustBeOpen": False, "d7Window": {"from": 5, "to": 7}}  # 코·눈 안내의 '보통 5~7일 뒤'

    # 진료시간 값은 00_policy 문장에서 읽은 것과 같다
    v02 = json.loads(re.search(r"```json\n(.*?)\n```", docs["hours-location.md"], re.S).group(1))
    hours = clinic_hours()
    assert v02["weekly"] == hours["weekly"] and v02["lastEntryMinutesBeforeClose"] == hours["lastEntryMinutesBeforeClose"]
    assert "[가정]" not in docs["hours-location.md"]  # 승인 문서는 출처가 있는 값만

    # 수술 확정 문자는 '하루 전까지 취소'를 약속하지 않고, 읽어 줄 문장은 실밥 제거를 확정처럼 말하지 않는다
    for key in ("surgery-half", "surgery-full"):
        assert "하루 전까지" not in b01["templates"][f"confirm-{key}"] and "서면 안내" in b01["templates"][f"confirm-{key}"]
    assert "하루 전까지" in b01["templates"]["confirm-first-consult"]
    assert b01["templates"]["readout-service-surgery-half"].startswith("보통 수술 5~7일 뒤")
    assert all("함께 보내드립니다" not in t for t in b01["templates"].values())  # 틀에 주소가 없으니 '함께'라고 하지 않는다

    # 샘플의원(모발 의원) 값을 말없이 물려받지 않는다
    assert b01["surgeryCancelNoticeDays"] == 0 and "careRoom" not in b01["resources"] and "careRoom" not in b01["capacity"]["base"]
    assert "모발" not in docs["booking-slots.md"] and "두피" not in docs["booking-slots.md"]
    assert "[[price-list]]" not in docs["ad-expression-guide.md"] and "00_policy" in docs["ad-expression-guide.md"]
    assert not any(re.search(r"^category:", text, re.M) for text in docs.values())


VENDORED_RULES = ROOT / "vendor/binjari/rules/index.html"


@pytest.mark.skipif(not VENDORED_RULES.exists(), reason="빈자리 빌드가 없다(scripts/vendor_binjari.sh)")
def test_vendored_binjari_accepted_the_rules():
    html = VENDORED_RULES.read_text(encoding="utf-8")
    assert "규칙 오류" not in html and "데모성형외과" in html
