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


def test_binjari_is_served_under_its_own_path(hub):
    from hub.app import ROOT

    client = TestClient(create_app(hub))
    page = client.get("/binjari/")
    if not (ROOT / "vendor" / "binjari").exists():
        assert page.status_code == 404  # 빌드를 넣지 않았으면 탭만 비고 앱은 그대로 돈다
        return
    assert page.status_code == 200 and "/binjari/_next/" in page.text
    asset = page.text.split('src="/binjari/_next/', 1)[1].split('"', 1)[0]
    assert client.get(f"/binjari/_next/{asset}").status_code == 200
    assert client.get("/binjari/rules/").status_code == 200
    assert "한창구" in client.get("/").text
