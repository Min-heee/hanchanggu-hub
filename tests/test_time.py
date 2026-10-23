from datetime import datetime, timedelta, timezone

from hub.models import now_kst


def test_clinic_clock_is_korean_time_whatever_the_server_timezone():
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs(now_kst() - utc_now - timedelta(hours=9)) < timedelta(minutes=1)
