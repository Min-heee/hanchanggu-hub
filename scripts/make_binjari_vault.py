"""빈자리가 읽을 데모성형외과 규칙 문서(볼트)를 만든다. scripts/vendor_binjari.sh가 빌드 직전에 부른다.

빈자리 엔진은 V01·V02·V04~V09와 B01(예약 칸 규칙)이 꼭 있어야 하고, 모르는 키나 빈 값은 거부한다.
- 진료시간(V02)과 실밥 제거 시기는 한창구 안내 자료(data/knowledge)의 문장에서 읽는다(hub/binjari_rules.py). 문장이 바뀌어
  읽지 못하면 멈춘다. 안내 문서(V04~V09)는 안내 자료 본문을 옮긴다.
- B01은 빈자리 B01의 칸 구조를 바탕으로, 사람·방·안건·문장을 데모성형외과 값으로 모두 바꾼다. 샘플의원(모발 의원)의
  값을 말없이 물려받지 않도록, 쓰지 않는 주사 회차 규칙 같은 필수 칸은 '이 의원에는 없음'으로 채운다.
  소요 시간·사람·방 수·수술 최소 준비 기간은 [가정]이고 B01 본문에 그렇게 적는다.
- B02(공휴일)는 의원과 무관해서 빈자리 것을 그대로 쓴다. V15(광고 표현)는 금지·주의 단어는 그대로 두고 본문만 이 의원 것으로 쓴다.
- effective 날짜는 빈자리 번들의 기준일(2026-09-21) 이전이어야 규칙 화면(/rules)이 문서를 고른다.

사용: python3 -I scripts/make_binjari_vault.py <빈자리 소스 폴더> <만들 볼트 폴더>
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

HUB = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HUB))
from hub.binjari_rules import (  # noqa: E402  (scripts/ 밖의 한창구 모듈을 쓴다)
    BUFFERS, CAPACITY, CLINIC, DAILY_MAX, PHONE, RESOURCES, SEARCH_DAYS, SERVICES, SURGERY_MIN_DAYS_AHEAD, clinic_hours, stitch_window,
)

DISCLAIMER = "가상 의원의 예시 문서이며 실제 의료 지침이 아닙니다."
EFFECTIVE = "2026-07-01"
JSON_BLOCK = re.compile(r"```json\n(.*?)\n```", re.S)


def front(doc_id: str, title: str, doc_type: str = "reference", status: str = "approved", extra: str = "") -> str:
    return (f"---\nid: {doc_id}\ntitle: {title}\ntype: {doc_type}\nversion: 1\nstatus: {status}\n"
            f"effective: {EFFECTIVE}\nowner: 원무팀\nfictional: true\n{extra}---\n\n# {title}\n\n")


def doc(doc_id: str, title: str, body: str, values=None, **kw) -> str:
    text = front(doc_id, title, **kw) + body.strip() + "\n\n"
    if values is not None:
        text += "## 기계가 읽는 값\n\n```json\n" + json.dumps(values, ensure_ascii=False, indent=2) + "\n```\n\n"
    return text + DISCLAIMER + "\n"


def knowledge(name: str) -> str:
    """안내 자료 본문. 제목 줄과 한창구 검색용 'category:' 줄은 뺀다."""
    body = (HUB / "data/knowledge" / name).read_text(encoding="utf-8").split("\n", 3)[3]
    return "\n".join(line for line in body.splitlines() if not re.match(r"^category:\s", line))


def hours_values() -> tuple[str, dict]:
    h = clinic_hours()
    return h["sentence"], {
        "timezone": "Asia/Seoul",
        "weekly": h["weekly"],
        "lunch": h["lunch"],
        "lastEntryMinutesBeforeClose": h["lastEntryMinutesBeforeClose"],
        "closed": ["sun", "public-holiday"],
        "extraClosedDates": [],
        "noSurgeryDays": h["noSurgeryDays"],
    }


def slot_rules(base: dict) -> dict:
    """빈자리 B01의 칸 구조에 데모성형외과 값을 넣는다."""
    first, last = stitch_window()
    rules = dict(base)
    rules.update({
        "clinicPhone": PHONE,
        "resources": RESOURCES,
        "capacity": {"base": CAPACITY, "overrides": []},
        "buffers": BUFFERS,
        "services": SERVICES,
        "searchDays": SEARCH_DAYS,
        "surgeryMinDaysAhead": SURGERY_MIN_DAYS_AHEAD,
        "surgeryCancelNoticeDays": 0,  # 안내 자료에 '며칠 안 수술' 취소 규칙이 없다 → 표시하지 않음
        "surgeryFollowUps": {"nextDayMustBeOpen": False, "d7Window": {"from": first, "to": last}},
        # 이 의원에는 주사 회차 프로그램이 없다. 엔진이 필수로 받는 칸이라 '없음'으로 채운다.
        "injectionMoveWindow": {"before": 0, "after": 0, "outsideNote": "이 의원에는 주사 회차 프로그램이 없음"},
        "dailyMax": DAILY_MAX,
        "scopeLine": "예약 변경·수술 후 경과 진료 → CRM 예약표에서 확인(이 도구 밖)",
    })
    rules["intake"] = [
        {"key": "consult", "label": "상담", "question": "처음 오시나요?", "answers": [
            {"key": "first", "label": "예, 처음", "service": "first-consult"},
            {"key": "returning", "label": "아니오, 재진", "service": "revisit-consult"}]},
        {"key": "procedure", "label": "시술", "subs": [
            {"key": "surgery", "label": "수술", "question": "원장 상담을 받으셨나요?", "answers": [
                {"key": "yes", "label": "예", "service": "surgery-half", "sizes": ["surgery-half", "surgery-full"]},
                {"key": "no", "label": "아니오", "service": "first-consult", "redirect": "처음 오시는 분은 원장 상담부터(00_policy)"}]},
            {"key": "petit", "label": "쁘띠(필러·보톡스)", "question": "이 병원에서 받으신 적 있나요?", "answers": [
                {"key": "new", "label": "처음", "service": "first-consult", "redirect": "처음 오시는 분은 원장 상담부터[가정]"},
                {"key": "again", "label": "예, 다시", "service": "petit"}]}]},
        {"key": "retouch", "label": "재수술", "hint": "재수술 · 리터치", "question": "여기서 수술 받으셨나요?", "answers": [
            {"key": "here", "label": "예, 여기서", "service": "revisit-consult", "notes": ["집도 원장 일정은 CRM에서 확인"]},
            {"key": "elsewhere", "label": "다른 병원", "service": "first-consult", "redirect": "다른 병원 수술은 첫 상담부터[가정]"}]},
    ]
    close = "확정 연락(카카오톡이나 문자)을 드려야 예약이 확정됩니다."
    hold = "{{date}} {{start}}~{{end}} {{service}} · 확정 연락 대기(빈자리 가예약, {{until}}까지) · 메모: {{memo}}"
    # 00_policy: 상세 주소와 주차 안내는 예약 확정 문자로 함께 보낸다 — 틀에는 주소가 없으니 이어서 보낸다고만 적는다.
    directions = "상세 주소와 주차 안내는 이어서 보내드립니다."
    templates = {
        "readout-open": "지금 비어 있는 시간을 말씀드릴게요.",
        "readout-two": "{{slot1}}, 또는 {{slot2}}에 자리가 있습니다.",
        "readout-one": "{{slot1}}에 자리가 있습니다.",
        "readout-service-first-consult": "첫 상담은 약 {{minutes}}분 걸리고, 원장 상담과 실장 상담을 함께 합니다.",
        "readout-service-revisit-consult": "원장 상담은 약 {{minutes}}분 걸립니다.",
        "readout-service-petit": "시술은 약 {{minutes}}분 걸립니다.",
        # 안내 자료: 절개 수술은 '보통' N~M일 뒤 실밥·부목 제거, 매몰법은 다르다 → 확정처럼 말하지 않는다
        "readout-service-surgery-half": f"보통 수술 {first}~{last}일 뒤 실밥·부목 제거로 내원하시는데, 수술 방법에 따라 달라 상담 때 안내드립니다.",
        "readout-service-surgery-full": "수술 일정은 원장 상담 내용에 맞춰 다시 확인드립니다.",
        "readout-close-deposit": close, "readout-close-deposit-near": close, "readout-close-no-deposit": close,
        "readout-close-surgery": "의료진 일정을 확인한 뒤 확정 연락(카카오톡이나 문자)을 드리고, 수술 예약금과 변경·환불 기준은 서면으로 안내드립니다.",
        "readout-closed": "{{range}} 휴진입니다({{reason}}). 가장 가까운 빈 시간은 {{slot1}}입니다.",
        "readout-closed-none": "{{range}} 휴진입니다({{reason}}).",
        "account-placeholder": "예약금 안내는 상담 때 서면으로",
        "hold-line": hold,
        "hold-line-no-deposit": hold,
    }
    for key, service in SERVICES.items():
        if not service["bookable"]:
            continue
        if service["earliest"] == "surgery":  # 수술 변경·환불은 서면 기준을 따른다(00_policy) — '하루 전까지'라고 쓰지 않는다
            change = "수술 일정 변경과 환불은 상담 때 드린 서면 안내를 따릅니다. 문의는 {{clinicPhone}}으로 해 주세요."
        else:
            change = "예약 변경과 취소는 방문 하루 전까지 {{clinicPhone}}으로 연락 주세요."
        templates[f"confirm-{key}"] = (f"안녕하세요, {CLINIC}입니다. {{{{date}}}} {{{{time}}}} {service['label']} 예약이 확정되었습니다. "
                                       f"{directions} {change} 감사합니다.")
    rules["templates"] = templates
    return rules


def ad_guide(source: str) -> str:
    """V15: 금지·주의 단어(기계가 읽는 값)는 빈자리 것 그대로, 본문은 이 의원 기준으로."""
    values = json.loads(JSON_BLOCK.search(source).group(1))
    body = ("환자에게 보내는 안내와 확정 문자에 효과를 약속하거나 다른 병원과 비교하는 표현을 쓰지 않습니다. "
            "금액은 메신저나 전화로 안내하지 않으며(00_policy.md), 할인·이벤트 가격으로 권유하지 않습니다. "
            "빈자리는 아래 단어가 들어간 문장 틀을 거부하거나 경고합니다.")
    return doc("V15", "의료광고 표현 가이드", body, values, doc_type="policy")


def main(binjari: Path, out: Path) -> None:
    # 볼트를 제자리에서 바꿀 수도 있으니(out = 빈자리 사본의 vault) 빈자리 문서를 먼저 다 읽어 둔다.
    base = json.loads(JSON_BLOCK.search((binjari / "vault/booking-slots.md").read_text(encoding="utf-8")).group(1))
    holidays = (binjari / "vault/holidays.md").read_text(encoding="utf-8")
    ads = (binjari / "vault/ad-expression-guide.md").read_text(encoding="utf-8")
    sentence, hours = hours_values()
    first, last = stitch_window()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    files = {
        "holidays.md": holidays,  # B02 공휴일: 의원과 무관
        "ad-expression-guide.md": ad_guide(ads),
        "about.md": doc("V01", f"{CLINIC} 소개", f"{CLINIC}는 한창구 데모용 가상 성형외과입니다. 실제 병원이 아닙니다. "
                        "코성형, 눈성형, 안면윤곽, 쁘띠 시술을 합니다. 치료 결정은 원장 상담 뒤에 합니다."),
        "hours-location.md": doc("V02", "진료시간·휴진", f"{sentence} 일요일과 공휴일은 휴진합니다(00_policy.md).",
                                 hours, doc_type="patient-guide"),
        "price-policy.md": doc("V03", "가격 안내 원칙", "메신저나 전화로는 금액을 안내하지 않습니다(00_policy.md). "
                               "그래서 빈자리 읽어 줄 문장에 금액이 없습니다.", []),
        "booking-policy.md": doc("V04", "예약 변경과 취소", knowledge("00_policy.md"), doc_type="policy"),
        "consultation.md": doc("V05", "상담 예약", "상담은 예약제로 운영합니다(00_policy.md).", doc_type="patient-guide"),
        "nose-surgery.md": doc("V06", "코성형 안내", knowledge("10_nose.md"), doc_type="patient-guide"),
        "aftercare.md": doc("V07", "수술 후 관리", knowledge("40_aftercare.md"), doc_type="patient-guide"),
        "contour-petit.md": doc("V08", "안면윤곽·쁘띠 안내", knowledge("30_contour_petit.md"), doc_type="patient-guide"),
        "eye-surgery.md": doc("V09", "눈성형 안내", knowledge("20_eye.md"), doc_type="patient-guide"),
        "booking-slots.md": doc(
            "B01", f"예약 칸 규칙(빈자리 · {CLINIC})",
            "AI가 쓴 [가정] 문서입니다. 진료시간은 V02(00_policy.md)에서, 실밥·부목 제거 시기(수술 "
            f"{first}~{last}일 뒤)는 V06·V09에서 읽습니다. 그 밖의 값은 병원 확인 전 [가정]입니다: 안건별 소요 시간과 단계, "
            f"사람·방 수, 수술실 정리 30분, 수술은 오늘부터 {SURGERY_MIN_DAYS_AHEAD}일 뒤부터(수술 전 검사·주의사항 안내 기간), "
            "첫 상담 하루 6건까지. 예약금은 받지 않고, 수술 예약금·변경·환불은 상담 때 서면으로 안내합니다(00_policy.md). "
            "주사 회차 프로그램은 이 의원에 없습니다.",
            slot_rules(base), doc_type="policy", status="draft", extra="assumption: true\napprovedBy: none\n"),
    }
    for name, text in files.items():
        (out / name).write_text(text, encoding="utf-8")
    print(f"볼트 {len(list(out.iterdir()))}편 → {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
