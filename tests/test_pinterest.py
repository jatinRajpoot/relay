import base64
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from relay.app import create_app
from relay.providers import PINTEREST_SCOPES

KEY = "test-service-key-with-at-least-32-characters"
HEADERS = {"X-API-Key": KEY}


def mock_pinterest_transport(request: httpx.Request):
    url = request.url
    path = url.path

    if path == "/v5/oauth/token":
        auth_header = request.headers.get("authorization", "")
        assert auth_header.startswith("Basic ")
        decoded = base64.b64decode(auth_header.split(" ", 1)[1]).decode()
        assert decoded == "test-pin-client:test-pin-secret"
        form = parse_qs(request.content.decode())
        if form.get("grant_type") == ["refresh_token"]:
            assert form["refresh_token"] == ["pin-refresh-token"]
            return httpx.Response(
                200,
                json={
                    "access_token": "pin-renewed-token",
                    "refresh_token": "pin-refresh-token",
                    "expires_in": 2592000,
                    "scope": PINTEREST_SCOPES,
                },
            )
        assert form.get("grant_type") == ["authorization_code"]
        assert form.get("code") == ["test-pin-code"]
        return httpx.Response(
            200,
            json={
                "access_token": "pin-access-token",
                "refresh_token": "pin-refresh-token",
                "expires_in": 2592000,
                "scope": PINTEREST_SCOPES,
            },
        )

    if path == "/v5/user_account":
        assert request.headers.get("authorization") in {
            "Bearer pin-access-token",
            "Bearer pin-renewed-token",
        }
        return httpx.Response(
            200,
            json={
                "id": "1642492_user",
                "username": "relay_pinner",
                "profile_image": "https://i.pinimg.com/avatar.jpg",
                "pin_count": 25,
                "board_count": 2,
                "follower_count": 500,
                "monthly_views": 12500,
            },
        )

    if path == "/v5/boards":
        if request.method == "POST":
            data = request.read().decode()
            import json
            body = json.loads(data)
            return httpx.Response(
                201,
                json={
                    "id": "board-new-123",
                    "name": body.get("name", "New Board"),
                    "privacy": body.get("privacy", "PUBLIC"),
                },
            )
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "board-main-1",
                        "name": "Relay Pins",
                        "pin_count": 12,
                        "privacy": "PUBLIC",
                    }
                ]
            },
        )

    if path == "/v5/pins":
        import json
        body = json.loads(request.read().decode())
        assert body["board_id"]
        assert body["media_source"]["source_type"] == "image_url"
        return httpx.Response(
            201,
            json={
                "id": "pin-987654",
                "title": body.get("title", ""),
                "description": body.get("description", ""),
                "board_id": body["board_id"],
                "link": body.get("link"),
            },
        )

    if path == "/v5/pins/pin-987654":
        return httpx.Response(
            200,
            json={"id": "pin-987654", "title": "Test Pin", "link": "https://example.com"},
        )

    if path == "/v5/user_account/analytics":
        return httpx.Response(
            200,
            json={
                "all": {
                    "IMPRESSION": 3400,
                    "SAVE": 120,
                    "PIN_CLICK": 230,
                }
            },
        )

    raise AssertionError(f"Unhandled mock Pinterest path: {path}")


def connect_pinterest(client):
    start = client.post("/oauth/pinterest/authorize", headers=HEADERS)
    assert start.status_code == 200
    query = parse_qs(urlsplit(start.json()["authorization_url"]).query)
    response = client.get(
        "/oauth/pinterest/callback",
        params={"state": query["state"][0], "code": "test-pin-code"},
    )
    assert response.status_code == 200, response.text
    return response.json()["account"], query


def test_pinterest_oauth_authorize_and_connect(settings):
    app = create_app(settings, httpx.MockTransport(mock_pinterest_transport))
    with TestClient(app) as client:
        start = client.post("/oauth/pinterest/authorize", headers=HEADERS)
        assert start.status_code == 200
        auth_url = start.json()["authorization_url"]
        assert auth_url.startswith("https://www.pinterest.com/oauth/")
        query = parse_qs(urlsplit(auth_url).query)
        assert query["client_id"] == ["test-pin-client"]
        assert query["scope"] == [PINTEREST_SCOPES]
        assert "state" in query

        account, _ = connect_pinterest(client)
        assert account["provider"] == "pinterest"
        assert account["name"] == "relay_pinner"
        assert account["external_id"] == "1642492_user"
        assert account["avatar_url"] == "https://i.pinimg.com/avatar.jpg"


def test_pinterest_token_refresh(settings):
    app = create_app(settings, httpx.MockTransport(mock_pinterest_transport))
    with TestClient(app) as client:
        account, _ = connect_pinterest(client)
        refreshed = client.post(f"/accounts/{account['id']}/refresh", headers=HEADERS)
        assert refreshed.status_code == 200
        assert refreshed.json()["account"]["id"] == account["id"]


def test_pinterest_boards_list_and_create(settings):
    app = create_app(settings, httpx.MockTransport(mock_pinterest_transport))
    with TestClient(app) as client:
        account, _ = connect_pinterest(client)
        boards_resp = client.get(f"/pinterest/boards?account_id={account['id']}", headers=HEADERS)
        assert boards_resp.status_code == 200
        boards = boards_resp.json()["boards"]
        assert len(boards) == 1
        assert boards[0]["id"] == "board-main-1"
        assert boards[0]["name"] == "Relay Pins"

        create_resp = client.post(
            "/pinterest/boards",
            json={
                "account_id": account["id"],
                "name": "New Board",
                "description": "Board for recipes",
                "privacy": "PUBLIC",
            },
            headers=HEADERS,
        )
        assert create_resp.status_code == 201
        assert create_resp.json()["board"]["id"] == "board-new-123"
        assert create_resp.json()["board"]["name"] == "New Board"


def test_pinterest_create_pin_idempotency_and_history(settings):
    app = create_app(settings, httpx.MockTransport(mock_pinterest_transport))
    with TestClient(app) as client:
        account, _ = connect_pinterest(client)
        pin_data = {
            "account_id": account["id"],
            "board_id": "board-main-1",
            "title": "Stunning Mountain Sunset",
            "description": "Golden hour glow in the Alps #mountains #travel",
            "link": "https://example.com/mountain-sunset",
            "media_url": "https://storage.googleapis.com/test-bucket/sunset.jpg",
            "idempotency_key": "pin-key-unique-12345",
        }
        res = client.post("/pinterest/pins", json=pin_data, headers=HEADERS)
        assert res.status_code == 201, res.text
        data = res.json()
        assert data["id"] == "pin-987654"
        assert data["url"] == "https://www.pinterest.com/pin/pin-987654/"
        assert data["attempt"]["status"] == "published"
        assert data["attempt"]["provider"] == "pinterest"

        # Idempotent replay with same key returns existing receipt without re-dispatch
        replay = client.post("/pinterest/pins", json=pin_data, headers=HEADERS)
        assert replay.status_code in {200, 201}
        assert replay.json()["id"] == "pin-987654"

        # Key reused with different title fails with 409
        conflicting = dict(pin_data, title="Different Title")
        conflict_res = client.post("/pinterest/pins", json=conflicting, headers=HEADERS)
        assert conflict_res.status_code == 409

        # Check post history
        history_res = client.get("/posts", headers=HEADERS)
        assert history_res.status_code == 200
        posts = history_res.json()["posts"]
        assert any(p["provider"] == "pinterest" and p["platform_post_id"] == "pin-987654" for p in posts)


def test_pinterest_create_pin_auto_board(settings):
    app = create_app(settings, httpx.MockTransport(mock_pinterest_transport))
    with TestClient(app) as client:
        account, _ = connect_pinterest(client)
        # Omit board_id; it auto-resolves to 'Relay Pins'
        pin_data = {
            "account_id": account["id"],
            "title": "Auto Board Pin",
            "description": "Auto board test",
            "media_url": "https://storage.googleapis.com/test-bucket/photo.jpg",
        }
        res = client.post("/pinterest/pins", json=pin_data, headers=HEADERS)
        assert res.status_code == 201
        assert res.json()["id"] == "pin-987654"


def test_pinterest_analytics(settings):
    app = create_app(settings, httpx.MockTransport(mock_pinterest_transport))
    with TestClient(app) as client:
        account, _ = connect_pinterest(client)
        res = client.get("/analytics?days=30", headers=HEADERS)
        assert res.status_code == 200
        data = res.json()
        pin_acc = next((a for a in data["accounts"] if a["account_id"] == account["id"]), None)
        assert pin_acc is not None
        assert pin_acc["provider"] == "pinterest"
        assert pin_acc["channel"]["followers"] == 500
        assert pin_acc["channel"]["pins"] == 25
        assert pin_acc["period"]["impressions"] == 3400
        assert pin_acc["period"]["saves"] == 120
        assert pin_acc["period"]["pin_clicks"] == 230
