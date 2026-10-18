import sqlite3
import time
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi.testclient import TestClient
from test_frontend import ORIGIN
from test_service import HEADERS, youtube_provider

from relay.app import create_app
from relay.store import Store

PASSWORD = "a long test password"


def signup(client, email="alex@example.com"):
    result = client.post("/auth/signup", headers=ORIGIN, json={
        "email": email, "password": PASSWORD, "name": "Alex", "workspace_name": "Studio",
    })
    assert result.status_code == 201, result.text
    return client.get("/session").json()


def connect(client):
    start = client.post("/oauth/youtube/authorize", headers=ORIGIN)
    assert start.status_code == 200
    state = parse_qs(urlsplit(start.json()["authorization_url"]).query)["state"][0]
    response = client.get("/oauth/youtube/callback", params={"state": state, "code": "test"})
    assert response.status_code == 200, response.text
    return response.json()["account"]


def test_landing_auth_pages_and_dashboard_gate(settings):
    with TestClient(create_app(settings)) as client:
        assert "Less juggling." in client.get("/").text
        assert 'href="/signup"' in client.get("/").text
        assert "account-form" in client.get("/login").text
        assert "account-form" in client.get("/signup").text
        assert client.get("/app", follow_redirects=False).headers["location"] == "/login"
        signup(client)
        assert "workspace-select" in client.get("/app").text
        assert client.get("/login", follow_redirects=False).headers["location"] == "/app"


def test_signup_login_normalization_password_hash_and_revoked_logout(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        state = signup(client, "Alex@Example.com")
        assert state["user"]["email"] == "alex@example.com"
        assert state["workspace"]["name"] == "Studio"
        assert state["workspaces"] == [state["workspace"]]
        token = client.cookies.get("relay_session")
        with app.state.store.connection() as db:
            user = dict(db.execute("SELECT * FROM users").fetchone())
            session = dict(db.execute("SELECT * FROM user_sessions").fetchone())
        assert PASSWORD not in user["password_hash"]
        assert session["token_hash"] != token
        assert "password" not in str(state)
        assert client.delete("/session", headers=ORIGIN).status_code == 204
        client.cookies.set("relay_session", token)
        assert client.get("/accounts").status_code == 401
        client.cookies.clear()
        response = client.post("/auth/login", headers=ORIGIN, json={
            "email": "ALEX@example.com", "password": PASSWORD,
        })
        assert response.status_code == 200
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=lax" in response.headers["set-cookie"]
        assert token != client.cookies.get("relay_session")
        with app.state.store.connection() as db:
            db.execute("UPDATE user_sessions SET expires_at=?", (int(time.time()) - 1,))
        assert client.get("/accounts").status_code == 401


def test_credentials_validation_origin_and_throttling(settings):
    with TestClient(create_app(settings)) as client:
        assert client.post("/auth/signup", headers=ORIGIN, json={
            "email": "alex@example.com", "password": "short", "name": "Alex",
        }).status_code == 422
        signup(client)
        assert client.post("/auth/signup", headers=ORIGIN, json={
            "email": "alex@example.com", "password": PASSWORD, "name": "Alex",
        }).status_code == 409
        assert client.post("/auth/login", json={
            "email": "alex@example.com", "password": PASSWORD,
        }).status_code == 403
        for _ in range(8):
            response = client.post("/auth/login", headers=ORIGIN, json={
                "email": "alex@example.com", "password": "wrong",
            })
            assert response.status_code == 401
            assert response.json()["detail"] == "Email or password is incorrect."
        assert client.post("/auth/login", headers=ORIGIN, json={
            "email": "alex@example.com", "password": PASSWORD,
        }).status_code == 429


def test_users_workspaces_and_every_resource_are_isolated(settings):
    app = create_app(settings, httpx.MockTransport(youtube_provider))
    with TestClient(app) as alice, TestClient(app) as bob:
        alice_state = signup(alice)
        first = connect(alice)
        store = app.state.store
        scope = store.workspace.set(alice_state["workspace"]["id"])
        try:
            item = store.create_content_item("clip.mp4", "video/mp4", 12, "clip", "https://cdn.test/clip")
            post = store.create_post(first["id"], "container")
            store.save_youtube_history(first["id"], "video", "Title", "", "clip.mp4",
                                       "video/mp4", None, "https://youtu.be/video", "private")
        finally:
            store.workspace.reset(scope)
        assert len(alice.get("/content").json()["items"]) == 1
        assert len(alice.get("/posts").json()["posts"]) == 1
        signup(bob, "bob@example.com")
        assert bob.get("/accounts").json()["accounts"] == []
        assert bob.get("/content").json()["items"] == []
        assert bob.get("/posts").json()["posts"] == []
        for method, path in [
            ("GET", f"/instagram/posts/{post['id']}"),
            ("POST", f"/instagram/posts/{post['id']}/publish"),
            ("POST", f"/accounts/{first['id']}/refresh"),
            ("DELETE", f"/accounts/{first['id']}"),
            ("POST", f"/workspaces/{alice_state['workspace']['id']}/select"),
        ]:
            assert bob.request(method, path, headers=ORIGIN).status_code == 404
        assert bob.post("/youtube/videos", headers=ORIGIN, data={
            "account_id": first["id"], "title": "Test", "content_id": item["id"],
        }).status_code == 404
        assert bob.get("/accounts", headers={
            "X-Workspace-ID": alice_state["workspace"]["id"],
        }).status_code == 404
        second = connect(bob)  # Same provider channel can belong to different workspaces.
        assert second["id"] != first["id"]
        assert alice.get("/accounts").json()["accounts"][0]["id"] == first["id"]
        assert alice.get("/accounts", headers=HEADERS).json()["accounts"] == []
        workspace = alice.post("/workspaces", json={"name": "Brand B"}, headers=ORIGIN).json()
        assert alice.post(f"/workspaces/{workspace['id']}/select", headers=ORIGIN).status_code == 200
        assert alice.get("/accounts").json()["accounts"] == []
        assert alice.get("/content").json()["items"] == []
        assert alice.get("/posts").json()["posts"] == []
        # A tab pinned to the original workspace keeps using it after another tab switches.
        assert alice.get("/accounts", headers={
            "X-Workspace-ID": alice_state["workspace"]["id"],
        }).json()["accounts"][0]["id"] == first["id"]
        assert len(alice.get("/workspaces").json()["workspaces"]) == 2
        assert len(bob.get("/workspaces").json()["workspaces"]) == 1


def test_oauth_completion_is_bound_to_original_workspace(settings):
    with TestClient(create_app(settings, httpx.MockTransport(youtube_provider))) as client:
        original = signup(client)["workspace"]["id"]
        start = client.post("/oauth/youtube/authorize", headers=ORIGIN)
        state = parse_qs(urlsplit(start.json()["authorization_url"]).query)["state"][0]
        workspace = client.post("/workspaces", json={"name": "Second"}, headers=ORIGIN).json()
        client.post(f"/workspaces/{workspace['id']}/select", headers=ORIGIN)
        assert client.get("/oauth/youtube/callback", params={"state": state, "code": "test"}).status_code == 200
        assert client.get("/accounts").json()["accounts"] == []
        client.post(f"/workspaces/{original}/select", headers=ORIGIN)
        assert len(client.get("/accounts").json()["accounts"]) == 1


def test_legacy_database_migration_preserves_connections_and_foreign_keys(settings):
    # Model a real pre-account database including dependent published jobs.
    with sqlite3.connect(settings.database_path) as db:
        db.executescript("""
            CREATE TABLE accounts (
                id TEXT PRIMARY KEY, provider TEXT NOT NULL, external_id TEXT NOT NULL,
                name TEXT NOT NULL, access_token TEXT NOT NULL, refresh_token TEXT,
                expires_at INTEGER NOT NULL, scopes TEXT NOT NULL,
                token_updated_at INTEGER NOT NULL, created_at INTEGER NOT NULL,
                UNIQUE(provider, external_id)
            );
            INSERT INTO accounts VALUES ('old', 'instagram', 'remote', 'Old channel',
                'encrypted-token', NULL, 9999999999, 'scope', 1, 1);
            CREATE TABLE instagram_posts (
                id TEXT PRIMARY KEY, account_id TEXT REFERENCES accounts(id) ON DELETE CASCADE,
                container_id TEXT NOT NULL UNIQUE, media_id TEXT, status TEXT, created_at INTEGER
            );
            INSERT INTO instagram_posts VALUES ('job', 'old', 'container', 'media', 'published', 1);
        """)
    store = Store(settings.database_path, settings.encryption_key)
    assert store.get_account("old")["workspace_id"] == "legacy"
    assert store.get_post("job")["status"] == "published"
    assert len(store.list_post_history()) == 1
    with store.connection() as db:
        assert list(db.execute("PRAGMA foreign_key_check")) == []
    assert store.delete_account("old")
    assert store.get_post("job") is None


def test_account_sessions_and_workspaces_survive_restart(settings):
    with TestClient(create_app(settings)) as client:
        original = signup(client)
        token = client.cookies.get("relay_session")
    with TestClient(create_app(settings)) as restarted:
        restarted.cookies.set("relay_session", token)
        current = restarted.get("/session").json()
        assert current["user"] == original["user"]
        assert current["workspaces"] == original["workspaces"]
