from fastapi.testclient import TestClient

from hub.app import create_app


def test_inbox_api_flow(hub):
    client = TestClient(create_app(hub))

    status = client.get("/api/status").json()
    assert status["mode"] == "offline" and status["metrics"]["open"] == 6

    threads = client.get("/api/threads").json()
    key = next(t["key"] for t in threads if t["name"] == "이민호")
    view = client.get(f"/api/threads/{key}").json()
    assert view["customer"]["id"] == "C002"

    flags = client.post("/api/check", json={"text": "부작용 전혀 없어요"}).json()["flags"]
    assert flags

    sent = client.post(f"/api/threads/{key}/approve", json={"text": view["analysis"]["draft_reply"]}).json()
    assert sent["similarity"] == 1.0
    assert sent["thread"]["status"] == "sent"
    assert client.get("/api/status").json()["metrics"]["kept_as_is"] == 1

    assert client.get("/api/threads/nope:1").status_code == 404
    assert client.get("/").status_code == 200


def test_inbox_and_recall_only_no_binjari_tab(hub):
    """한창구는 문의함과 리콜·팔로업 두 화면이다. 빈자리는 별도 프로젝트로 두고 여기 붙이지 않는다."""
    client = TestClient(create_app(hub))
    page = client.get("/").text
    assert 'data-view="inbox"' in page and 'data-view="recall"' in page and "binjari" not in page
    assert client.get("/binjari/").status_code == 404
    assert "한창구" in client.get("/").text
