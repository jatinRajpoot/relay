import sqlite3
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi.testclient import TestClient
from test_service import HEADERS, KEY, youtube_provider

from relay.app import create_app
from relay.store import Store

ORIGIN = {"Origin": "http://localhost:8000"}


def test_popup_oauth_callback_uses_completion_page(settings):
    with TestClient(create_app(settings, httpx.MockTransport(youtube_provider))) as client:
        assert unlock(client).status_code == 200
        start = client.post("/oauth/youtube/authorize?ui=true&popup=true", headers=ORIGIN)
        state = parse_qs(urlsplit(start.json()["authorization_url"]).query)["state"][0]
        result = client.get(
            "/oauth/youtube/callback",
            params={"state": state, "code": "test-code"},
            follow_redirects=False,
        )
        assert result.status_code == 303
        assert result.headers["location"] == "/oauth/complete?provider=youtube&result=connected"


def test_instagram_local_http_ui_authorize_explains_setup(settings):
    with TestClient(create_app(settings)) as client:
        response = client.post("/oauth/instagram/authorize?ui=true&popup=true", headers=HEADERS)
        assert response.status_code == 409
        assert "HTTPS" in response.json()["detail"]


def test_hosted_instagram_popup_uses_same_registered_callback(settings):
    settings = replace(settings, base_url="https://relay.example")
    with TestClient(create_app(settings), base_url="https://relay.example") as client:
        start = client.post("/oauth/instagram/authorize?ui=true&popup=true", headers=HEADERS)
        assert start.status_code == 200
        params = parse_qs(urlsplit(start.json()["authorization_url"]).query)
        assert params["redirect_uri"] == ["https://relay.example/oauth/instagram/callback"]
        assert "Secure" in start.headers["set-cookie"]
        assert client.get("/session").json()["providers"]["instagram"]["setup_issue"] is None


def test_popup_cancellation_returns_completion_with_no_tokens(settings):
    with TestClient(create_app(settings)) as client:
        start = client.post("/oauth/youtube/authorize?ui=true&popup=true", headers=HEADERS)
        state = parse_qs(urlsplit(start.json()["authorization_url"]).query)["state"][0]
        result = client.get(
            "/oauth/youtube/callback",
            params={"state": state, "error": "access_denied"},
            follow_redirects=False,
        )
        assert result.headers["location"] == "/oauth/complete?provider=youtube&result=cancelled"
        page = client.get(result.headers["location"])
        assert page.status_code == 200
        assert "oauth-complete.js" in page.text
        assert KEY not in page.text


def test_cross_origin_or_wrong_path_callbacks_fail_before_opening_provider(settings):
    for redirect in (
        "https://other.example/oauth/youtube/callback",
        "http://localhost:8000/wrong-path",
    ):
        configured = replace(settings, youtube_redirect_uri=redirect)
        with TestClient(create_app(configured)) as client:
            result = client.post("/oauth/youtube/authorize?ui=true&popup=true", headers=HEADERS)
            assert result.status_code == 409


def unlock(client):
    return client.post("/session", json={"service_key": KEY}, headers=ORIGIN)


def test_frontend_serves_packaged_assets_without_secrets(settings):
    with TestClient(create_app(settings)) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "Connect Instagram" in response.text
        assert "Connect YouTube" in response.text
        assert KEY not in response.text
        for asset in (
            "styles.css",
            "app.js",
            "favicon.svg",
            "oauth-popup.mjs",
            "oauth-complete.js",
        ):
            assert client.get(f"/static/{asset}").status_code == 200
        assert "javascript" in client.get("/static/oauth-popup.mjs").headers["content-type"]
        assert client.get("/static/../.env").status_code == 404


def test_session_unlock_access_cookie_flags_and_logout(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/session").json()["authenticated"] is False
        response = unlock(client)
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=lax" in cookie
        assert "Max-Age=28800" in cookie
        assert KEY not in cookie
        assert client.get("/session").json()["authenticated"] is True
        assert client.get("/accounts").status_code == 200
        assert client.post("/oauth/youtube/authorize", headers=ORIGIN).status_code == 200
        assert client.delete("/session", headers=ORIGIN).status_code == 204
        assert client.get("/session").json()["authenticated"] is False
        assert client.get("/accounts").status_code == 401


def test_unlock_and_cookie_mutations_require_same_origin(settings):
    with TestClient(create_app(settings)) as client:
        for headers in ({}, {"Origin": "https://attacker.example"}):
            assert (
                client.post("/session", json={"service_key": KEY}, headers=headers).status_code
                == 403
            )
        assert (
            client.post("/session", json={"service_key": "wrong"}, headers=ORIGIN).status_code
            == 401
        )
        assert unlock(client).status_code == 200
        assert client.post("/oauth/youtube/authorize").status_code == 403
        assert (
            client.delete("/session", headers={"Origin": "https://attacker.example"}).status_code
            == 403
        )
        # Existing API-key clients continue to work without a browser Origin.
        assert client.post("/oauth/youtube/authorize", headers=HEADERS).status_code == 200


def test_tampered_and_expired_sessions_are_rejected(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        client.cookies.set("relay_session", "tampered")
        assert client.get("/accounts").status_code == 401
        marker = app.state.store.cipher.encrypt_at_time(b"anything", current_time=1).decode()
        client.cookies.set("relay_session", marker)
        assert client.get("/accounts").status_code == 401


def test_frontend_oauth_callback_redirects_and_preserves_session(settings):
    app = create_app(settings, httpx.MockTransport(youtube_provider))
    with TestClient(app) as client:
        assert unlock(client).status_code == 200
        start = client.post("/oauth/youtube/authorize?ui=true", headers=ORIGIN)
        state = parse_qs(urlsplit(start.json()["authorization_url"]).query)["state"][0]
        response = client.get(
            "/oauth/youtube/callback",
            params={"state": state, "code": "test-code"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/?connected=youtube"
        assert "google-access" not in response.text
        assert len(client.get("/accounts").json()["accounts"]) == 1
        assert client.get("/session").json()["authenticated"] is True


def test_frontend_denial_and_provider_failure_redirect_to_dashboard(settings):
    def fail(request):
        return httpx.Response(400, json={"error": "invalid_grant"})

    with TestClient(create_app(settings, httpx.MockTransport(fail))) as client:
        assert unlock(client).status_code == 200
        for parameters, reason in (
            ({"error": "access_denied"}, "cancelled"),
            ({"code": "bad"}, "connection"),
        ):
            start = client.post("/oauth/youtube/authorize?ui=true", headers=ORIGIN)
            state = parse_qs(urlsplit(start.json()["authorization_url"]).query)["state"][0]
            response = client.get(
                "/oauth/youtube/callback",
                params={"state": state} | parameters,
                follow_redirects=False,
            )
            assert response.status_code == 303
            assert response.headers["location"] == f"/?oauth_error={reason}"


def test_existing_sqlite_oauth_schema_is_migrated(settings):
    with sqlite3.connect(settings.database_path) as db:
        db.execute(
            "CREATE TABLE oauth_states (state_hash TEXT PRIMARY KEY, provider TEXT NOT NULL, browser_hash TEXT NOT NULL, verifier TEXT, expires_at INTEGER NOT NULL)"
        )
    store = Store(settings.database_path, settings.encryption_key)
    state = store.create_state("youtube", "browser", "verifier", return_to_ui=True)
    assert store.consume_state(state, "youtube", "browser")["return_to_ui"] is True
