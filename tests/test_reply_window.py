import json
from datetime import datetime, time, timedelta

import pytest

from hub.reply_window import reply_window
from tests.conftest import TODAY


def at(day_offset, hour, minute=0):
    return datetime.combine(TODAY + timedelta(days=day_offset), time(hour, minute))


def test_instagram_window_states():
    last = at(0, 10, 2).isoformat(timespec="minutes")
    assert reply_window("instagram", last, at(0, 15)).state == "open"
    assert reply_window("instagram", last, at(1, 10, 1)).state == "open"  # 24시간 직전
    human = reply_window("instagram", last, at(1, 10, 2))  # 24시간 지난 순간부터 사람 상담원 답장
    assert human.state == "human_agent" and human.tag == "HUMAN_AGENT"
    assert human.deadline == at(7, 10, 2).isoformat(timespec="minutes")
    assert reply_window("instagram", last, at(7, 10, 1)).state == "human_agent"
    assert reply_window("instagram", last, at(7, 10, 2)).state == "closed"
    assert reply_window("kakao", last, at(30, 9)).state == "none"


def outbox_records(hub):
    path = hub.channels.outbox_path
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_inbox_reply_within_24h_has_no_tag(hub):
    hub.clock = lambda: at(0, 15)
    assert hub.thread_view("cust:C005")["reply_window"]["state"] == "open"
    hub.approve("cust:C005", "최유나님, 상담 일정을 확인해 드릴게요.")
    assert "tag" not in outbox_records(hub)[-1]


def test_inbox_reply_after_24h_goes_as_human_agent(hub):
    hub.clock = lambda: at(2, 11)
    view = hub.thread_view("cust:C005")
    assert view["reply_window"]["state"] == "human_agent"
    hub.approve("cust:C005", "최유나님, 늦게 답드려 죄송합니다.")
    record = outbox_records(hub)[-1]
    assert record["channel"] == "instagram" and record["tag"] == "HUMAN_AGENT"


def test_inbox_reply_after_7_days_is_blocked(hub):
    hub.clock = lambda: at(8, 9)
    assert hub.thread_view("cust:C005")["reply_window"]["state"] == "closed"
    with pytest.raises(ValueError, match="전화나 문자"):
        hub.approve("cust:C005", "답장")
    assert hub.thread_view("cust:C005")["status"] != "sent"
    # 다른 채널은 기한이 없다
    hub.approve("cust:C002", "이민호님, 답장드려요.")
    assert outbox_records(hub)[-1]["channel"] == "naver"
