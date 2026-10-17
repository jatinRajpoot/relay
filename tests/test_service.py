import base64
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from relay.app import create_app
from relay.providers import INSTAGRAM_SCOPES, YOUTUBE_SCOPES
from relay.store import Store

KEY = "test-service-key-with-at-least-32-characters"
HEADERS = {"X-API-Key": KEY}


def youtube_provider(request):
    if request.url.path == "/token":
        form = parse_qs(request.content.decode())
        assert form["client_secret"] == ["test-google-secret"]
        if form["grant_type"] == ["refresh_token"]:
            assert form["refresh_token"] == ["google-refresh"]
            return httpx.Response(200, json={"access_token": "google-renewed", "expires_in": 3600})
        assert form["code_verifier"]
        assert form["redirect_uri"] == ["http://localhost:8000/oauth/youtube/callback"]
        return httpx.Response(
            200,
            json={
                "access_token": "google-access",
                "refresh_token": "google-refresh",
                "expires_in": 3600,
                "scope": YOUTUBE_SCOPES,
            },
        )
    if request.url.path == "/youtube/v3/channels":
        assert request.headers["authorization"] == "Bearer google-access"
        return httpx.Response(
            200, json={"items": [{"id": "channel-1", "snippet": {"title": "My channel"}}]}
        )
    raise AssertionError(f"Unexpected provider path {request.url.path}")


def instagram_provider(request):
    if request.url.host == "api.instagram.com":
        assert request.method == "POST"
        assert "multipart/form-data" in request.headers["content-type"]
        assert b"test-ig-secret" in request.content
        return httpx.Response(
            200,
            json={
                "access_token": "ig-short",
                "user_id": "login-scoped-id",
                "permissions": INSTAGRAM_SCOPES.split(","),
            },
        )
    if request.url.path == "/access_token":
        assert request.url.params["grant_type"] == "ig_exchange_token"
        assert request.url.params["access_token"] == "ig-short"
        return httpx.Response(200, json={"access_token": "ig-long", "expires_in": 5184000})
    if request.url.path == "/v25.0/me":
        assert request.headers["authorization"] == "Bearer ig-long"
        return httpx.Response(200, json={"user_id": "professional-id", "username": "my_instagram"})
    raise AssertionError(f"Unexpected provider path {request.url.path}")


def connect(client, provider="youtube"):
    start = client.post(f"/oauth/{provider}/authorize", headers=HEADERS)
    assert start.status_code == 200
    query = parse_qs(urlsplit(start.json()["authorization_url"]).query)
    response = client.get(
        f"/oauth/{provider}/callback", params={"state": query["state"][0], "code": "test-code"}
    )
    assert response.status_code == 200, response.text
    return response.json()["account"], query


@pytest.mark.parametrize(
    "route,method",
    [
        ("/accounts", "GET"),
        ("/oauth/youtube/authorize", "POST"),
        ("/accounts/nope/refresh", "POST"),
        ("/accounts/nope", "DELETE"),
        ("/instagram/posts", "POST"),
        ("/instagram/posts/nope", "GET"),
        ("/instagram/posts/nope/publish", "POST"),
        ("/youtube/videos", "POST"),
    ],
)
def test_protected_routes(settings, route, method):
    with TestClient(create_app(settings)) as client:
        assert client.request(method, route).status_code == 401
        assert client.request(method, route, headers={"X-API-Key": "wrong"}).status_code == 401


def test_youtube_oauth_pkce_encryption_and_replay(settings):
    app = create_app(settings, httpx.MockTransport(youtube_provider))
    with TestClient(app) as client:
        account, query = connect(client)
        assert query["access_type"] == ["offline"]
        assert set(query["prompt"][0].split()) == {"consent", "select_account"}
        assert query["code_challenge_method"] == ["S256"]
        assert account["has_refresh_token"] is True
        assert "access_token" not in account and "refresh_token" not in account
        assert client.get("/accounts", headers=HEADERS).json()["accounts"] == [account]
        raw = app.state.store.get_account(account["id"])
        assert raw["access_token"] != "google-access"
        assert raw["refresh_token"] != "google-refresh"
        assert app.state.store.decrypt(raw["access_token"]) == "google-access"
        replay = client.get(
            "/oauth/youtube/callback", params={"state": query["state"][0], "code": "test-code"}
        )
        assert replay.status_code == 400
        assert replay.headers["cache-control"] == "no-store"
    # Persistence across application instances, with the same encryption key.
    with TestClient(create_app(settings)) as client:
        assert client.get("/accounts", headers=HEADERS).json()["accounts"][0]["id"] == account["id"]


def test_state_cookie_binding_and_wrong_provider(settings):
    app = create_app(settings, httpx.MockTransport(youtube_provider))
    with TestClient(app) as client:
        start = client.post("/oauth/youtube/authorize", headers=HEADERS)
        state = parse_qs(urlsplit(start.json()["authorization_url"]).query)["state"][0]
        cookies = list(client.cookies.jar)
        assert cookies[0].has_nonstandard_attr("HttpOnly")
        saved_cookie = cookies[0]
        client.cookies.clear()
        assert (
            client.get("/oauth/youtube/callback", params={"state": state, "code": "x"}).status_code
            == 400
        )
        # A wrong browser did not consume the original state.
        client.cookies.set(saved_cookie.name, saved_cookie.value, path=saved_cookie.path)
        assert (
            client.get("/oauth/youtube/callback", params={"state": state, "code": "x"}).status_code
            == 200
        )
        assert (
            client.get(
                "/oauth/instagram/callback", params={"state": state, "code": "x"}
            ).status_code
            == 400
        )


def test_state_is_hashed_expiring_atomic_and_pkce_encrypted(settings):
    store = Store(settings.database_path, settings.encryption_key)
    state = store.create_state("youtube", "browser-secret", "pkce-secret")
    with store.connection() as db:
        row = dict(db.execute("SELECT * FROM oauth_states").fetchone())
        assert row["state_hash"] != state
        assert row["browser_hash"] != "browser-secret"
        assert row["verifier"] != "pkce-secret"
    assert store.consume_state(state, "instagram", "browser-secret") is None
    assert store.consume_state(state, "youtube", "different-browser") is None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: store.consume_state(state, "youtube", "browser-secret"), range(2))
        )
    assert results.count(None) == 1
    assert {"verifier": "pkce-secret", "return_to_ui": False, "popup": False} in results
    expired = store.create_state("youtube", "b", "v")
    with store.connection() as db:
        db.execute("UPDATE oauth_states SET expires_at=?", (int(time.time()) - 1,))
    assert store.consume_state(expired, "youtube", "b") is None


def test_pkce_verifier_matches_authorization_challenge(settings):
    app = create_app(settings, httpx.MockTransport(youtube_provider))
    with TestClient(app) as client:
        start = client.post("/oauth/youtube/authorize", headers=HEADERS)
        query = parse_qs(urlsplit(start.json()["authorization_url"]).query)
        with app.state.store.connection() as db:
            encrypted = db.execute("SELECT verifier FROM oauth_states").fetchone()[0]
        verifier = app.state.store.decrypt(encrypted)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        assert query["code_challenge"] == [challenge]


def test_denial_and_missing_code_consume_state(settings):
    with TestClient(create_app(settings)) as client:
        for extra in ({"error": "access_denied"}, {}):
            url = client.post("/oauth/youtube/authorize", headers=HEADERS).json()[
                "authorization_url"
            ]
            state = parse_qs(urlsplit(url).query)["state"][0]
            response = client.get("/oauth/youtube/callback", params={"state": state} | extra)
            assert response.status_code == 400
            assert (
                client.get(
                    "/oauth/youtube/callback", params={"state": state, "code": "x"}
                ).status_code
                == 400
            )


def test_youtube_refresh_and_reconnect_preserve_refresh_token(settings):
    app = create_app(settings, httpx.MockTransport(youtube_provider))
    with TestClient(app) as client:
        account, _ = connect(client)
        response = client.post(f"/accounts/{account['id']}/refresh", headers=HEADERS)
        assert response.status_code == 200
        row = app.state.store.get_account(account["id"])
        assert app.state.store.decrypt(row["access_token"]) == "google-renewed"
        assert app.state.store.decrypt(row["refresh_token"]) == "google-refresh"
        row = app.state.store.save_account(
            "youtube",
            "channel-1",
            "Renamed",
            {
                "access_token": "reconnected",
                "expires_in": 3600,
            },
            YOUTUBE_SCOPES,
        )
        assert row["id"] == account["id"]
        assert app.state.store.decrypt(row["refresh_token"]) == "google-refresh"


def test_instagram_exchange_uses_professional_id(settings):
    app = create_app(settings, httpx.MockTransport(instagram_provider))
    with TestClient(app) as client:
        account, query = connect(client, "instagram")
        assert query["scope"] == [INSTAGRAM_SCOPES]
        assert account["external_id"] == "professional-id"
        assert account["has_refresh_token"] is False
        stored = app.state.store.get_account(account["id"])
        assert app.state.store.decrypt(stored["access_token"]) == "ig-long"
        # Refreshing a newly issued Instagram token is refused without an upstream call.
        assert client.post(f"/accounts/{account['id']}/refresh", headers=HEADERS).status_code == 409


def test_instagram_refresh_age_expiry_and_rotation(settings):
    calls = []

    def handler(request):
        if request.url.path == "/refresh_access_token":
            calls.append(request)
            assert request.url.params["grant_type"] == "ig_refresh_token"
            return httpx.Response(200, json={"access_token": "ig-renewed", "expires_in": 5184000})
        return instagram_provider(request)

    app = create_app(settings, httpx.MockTransport(handler))
    with TestClient(app) as client:
        account, _ = connect(client, "instagram")
        with app.state.store.connection() as db:
            db.execute("UPDATE accounts SET token_updated_at=?", (int(time.time()) - 86401,))
        assert client.post(f"/accounts/{account['id']}/refresh", headers=HEADERS).status_code == 200
        assert len(calls) == 1
        assert (
            app.state.store.decrypt(app.state.store.get_account(account["id"])["access_token"])
            == "ig-renewed"
        )
        with app.state.store.connection() as db:
            db.execute("UPDATE accounts SET expires_at=?", (int(time.time()) - 1,))
        response = client.post(f"/accounts/{account['id']}/refresh", headers=HEADERS)
        assert response.status_code == 409
        assert len(calls) == 1


@pytest.mark.parametrize("media_type,url_field", [("IMAGE", "image_url"), ("REELS", "video_url")])
def test_instagram_create_process_publish_and_repeat(settings, media_type, url_field):
    publish_calls = []
    polls = []

    def handler(request):
        if request.url.path == "/v25.0/professional-id/media":
            form = parse_qs(request.content.decode())
            assert form[url_field] == ["https://cdn.example/media"]
            assert request.headers["authorization"] == "Bearer ig-long"
            return httpx.Response(200, json={"id": "container-1"})
        if request.url.path == "/v25.0/container-1":
            polls.append(request)
            return httpx.Response(
                200, json={"status_code": "IN_PROGRESS" if len(polls) == 1 else "FINISHED"}
            )
        if request.url.path.endswith("/media_publish"):
            publish_calls.append(request)
            return httpx.Response(200, json={"id": "published-1"})
        return instagram_provider(request)

    app = create_app(settings, httpx.MockTransport(handler))
    with TestClient(app) as client:
        account, _ = connect(client, "instagram")
        response = client.post(
            "/instagram/posts",
            headers=HEADERS,
            json={
                "account_id": account["id"],
                "media_type": media_type,
                "media_url": "https://cdn.example/media",
                "caption": "Hello",
            },
        )
        assert response.status_code == 201
        post = response.json()
        assert post["status"] == "processing"
        path = f"/instagram/posts/{post['id']}"
        assert client.post(path + "/publish", headers=HEADERS).status_code == 409
        assert client.get(path, headers=HEADERS).json()["status"] == "ready"
        published = client.post(path + "/publish", headers=HEADERS)
        assert published.status_code == 200
        assert published.json()["media_id"] == "published-1"
        assert client.post(path + "/publish", headers=HEADERS).json() == published.json()
        assert len(publish_calls) == 1


def test_failed_publish_is_not_blindly_retried(settings):
    publish_calls = []

    def handler(request):
        if request.url.path.endswith("/media_publish"):
            publish_calls.append(request)
            raise httpx.ReadTimeout("secret-provider-url", request=request)
        if request.url.path == "/v25.0/container-1":
            return httpx.Response(200, json={"status_code": "FINISHED"})
        return instagram_provider(request)

    app = create_app(settings, httpx.MockTransport(handler))
    with TestClient(app) as client:
        account, _ = connect(client, "instagram")
        post = app.state.store.create_post(account["id"], "container-1")
        path = f"/instagram/posts/{post['id']}/publish"
        response = client.post(path, headers=HEADERS)
        assert response.status_code == 502
        assert "secret-provider-url" not in response.text
        assert app.state.store.get_post(post["id"])["status"] == "unknown"
        assert client.post(path, headers=HEADERS).status_code == 409
        assert len(publish_calls) == 1


def test_youtube_stream_upload_and_automatic_refresh(settings):
    def handler(request):
        if request.url.path == "/upload/youtube/v3/videos":
            assert request.headers["authorization"] == "Bearer google-renewed"
            if request.method == "POST":
                body = json.loads(request.content)
                assert body["status"]["privacyStatus"] == "private"
                assert body["snippet"]["title"] == "Test upload"
                assert request.headers["x-upload-content-length"] == "10"
                return httpx.Response(
                    200,
                    headers={
                        "location": "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=test"
                    },
                )
            assert request.method == "PUT"
            assert request.content == b"video-data"
            assert request.headers["content-length"] == "10"
            return httpx.Response(
                201, json={"id": "video-1", "status": {"privacyStatus": "private"}}
            )
        return youtube_provider(request)

    app = create_app(settings, httpx.MockTransport(handler))
    with TestClient(app) as client:
        account, _ = connect(client)
        with app.state.store.connection() as db:
            db.execute("UPDATE accounts SET expires_at=?", (int(time.time()) - 1,))
        result = client.post(
            "/youtube/videos",
            headers=HEADERS,
            data={
                "account_id": account["id"],
                "title": "Test upload",
            },
            files={"file": ("video.mp4", b"video-data", "video/mp4")},
        )
        assert result.status_code == 201, result.text
        assert result.json()["id"] == "video-1"
        assert result.json()["privacy_status"] == "private"


@pytest.mark.parametrize(
    "malicious_url",
    [
        "https://attacker.example/upload",
        "http://www.googleapis.com/upload/youtube/v3/videos",
        "https://www.googleapis.com.attacker.example/upload/youtube/v3/videos",
    ],
)
def test_upload_session_url_cannot_exfiltrate_token(settings, malicious_url):
    def handler(request):
        if request.url.path == "/upload/youtube/v3/videos":
            return httpx.Response(200, headers={"location": malicious_url})
        return youtube_provider(request)

    with TestClient(create_app(settings, httpx.MockTransport(handler))) as client:
        account, _ = connect(client)
        response = client.post(
            "/youtube/videos",
            headers=HEADERS,
            data={"account_id": account["id"], "title": "Test"},
            files={"file": ("x.mp4", b"data", "video/mp4")},
        )
        assert response.status_code == 502


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (400, {"error": "invalid_grant", "secret": "sensitive-token"}, 409),
        (400, {"error": {"code": 190, "message": "sensitive-token"}}, 409),
        (403, {"error": {"message": "sensitive-token"}}, 502),
        (200, {"access_token": "sensitive-token", "expires_in": -1}, 502),
    ],
)
def test_errors_do_not_leak_provider_tokens(settings, status, body, expected):
    def handler(request):
        return httpx.Response(status, json=body)

    app = create_app(settings, httpx.MockTransport(handler))
    with TestClient(app) as client:
        url = client.post("/oauth/youtube/authorize", headers=HEADERS).json()["authorization_url"]
        state = parse_qs(urlsplit(url).query)["state"][0]
        response = client.get("/oauth/youtube/callback", params={"state": state, "code": "x"})
        assert response.status_code == expected
        assert "sensitive-token" not in response.text
        assert app.state.store.list_accounts() == []


def test_missing_youtube_scope_is_rejected(settings):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "access_token": "a",
                "expires_in": 3600,
                "scope": "https://www.googleapis.com/auth/youtube.readonly",
            },
        )

    with TestClient(create_app(settings, httpx.MockTransport(handler))) as client:
        url = client.post("/oauth/youtube/authorize", headers=HEADERS).json()["authorization_url"]
        state = parse_qs(urlsplit(url).query)["state"][0]
        response = client.get("/oauth/youtube/callback", params={"state": state, "code": "x"})
        assert response.status_code == 409


def test_validation_wrong_account_media_and_disconnect(settings):
    app = create_app(settings, httpx.MockTransport(youtube_provider))
    with TestClient(app) as client:
        account, _ = connect(client)
        body = {
            "account_id": account["id"],
            "media_type": "IMAGE",
            "media_url": "https://cdn.example/photo.jpg",
        }
        assert client.post("/instagram/posts", headers=HEADERS, json=body).status_code == 404
        for url in (
            "file:///secret",
            "http://example.com/a.jpg",
            "https://user:pass@example.com/a.jpg",
        ):
            assert (
                client.post(
                    "/instagram/posts", headers=HEADERS, json=body | {"media_url": url}
                ).status_code
                == 422
            )
        assert (
            client.post(
                "/youtube/videos",
                headers=HEADERS,
                data={"account_id": account["id"], "title": "x"},
                files={"file": ("x.txt", b"x", "text/plain")},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/youtube/videos",
                headers=HEADERS,
                data={"account_id": account["id"], "title": "x"},
                files={"file": ("x.mp4", b"", "video/mp4")},
            ).status_code
            == 413
        )
        assert client.delete(f"/accounts/{account['id']}", headers=HEADERS).status_code == 204
        assert client.get("/accounts", headers=HEADERS).json() == {"accounts": []}
        assert client.post(f"/accounts/{account['id']}/refresh", headers=HEADERS).status_code == 404


def test_local_health_and_openapi(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json() == {"status": "ok"}
        schema = client.get("/openapi.json").json()
        assert schema["components"]["securitySchemes"]["APIKeyHeader"]["name"] == "X-API-Key"
        assert "/youtube/videos" in schema["paths"]
        assert not schema["paths"]["/oauth/{provider}/callback"]["get"].get("security")
