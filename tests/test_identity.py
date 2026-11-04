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
        assert 'href="/try"' in client.get("/").text
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
        # Eight failures are recorded. The rejected duplicate signup above is *not*
        # charged: it proves nothing about the password, and charging it would let
        # anyone lock a known address out with ten cheap signup posts.
        # A correct password still works and does not consume the remaining budget.
        assert client.post("/auth/login", headers=ORIGIN, json={
            "email": "alex@example.com", "password": PASSWORD,
        }).status_code == 200
        # The ninth and tenth failures are accepted; the next attempt is throttled.
        for _ in range(2):
            assert client.post("/auth/login", headers=ORIGIN, json={
                "email": "alex@example.com", "password": "wrong",
            }).status_code == 401
        assert client.post("/auth/login", headers=ORIGIN, json={
            "email": "alex@example.com", "password": PASSWORD,
        }).status_code == 429


def test_successful_logins_never_consume_the_throttle(settings):
    with TestClient(create_app(settings)) as client:
        signup(client)
        for _ in range(15):
            assert client.post("/auth/login", headers=ORIGIN, json={
                "email": "alex@example.com", "password": PASSWORD,
            }).status_code == 200


def test_stale_workspace_pin_serves_the_shell_and_falls_back_safely(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        state = signup(client)
        stale = {"X-Workspace-ID": "deleted-workspace"}
        # The HTML shell must still render instead of 404ing on a stale pin.
        assert client.get("/", headers=stale).status_code == 200
        assert client.get("/app", headers=stale).status_code == 200
        redirect = client.get("/login", headers=stale, follow_redirects=False)
        assert redirect.status_code == 303
        assert redirect.headers["location"] == "/app"
        # Bootstrap resolves to a workspace this user owns, never another user's.
        current = client.get("/session", headers=stale)
        assert current.status_code == 200
        assert current.json()["workspace"]["id"] == state["workspace"]["id"]
        # API routes stay strict: a pin the caller does not own is still refused.
        assert client.get("/accounts", headers=stale).status_code == 404
        assert client.get("/posts", headers=stale).status_code == 404


def test_stale_pin_fallback_cannot_reach_another_users_workspace(settings):
    app = create_app(settings, httpx.MockTransport(youtube_provider))
    with TestClient(app) as alice, TestClient(app) as bob:
        alice_state = signup(alice)
        alice_workspace = alice_state["workspace"]["id"]
        bob_state = signup(bob, "bob@example.com")
        # Bob pins a workspace that exists but belongs to Alice: that is tampering, not a
        # stale pin, so bootstrap refuses it instead of silently swapping in his own.
        response = bob.get("/session", headers={"X-Workspace-ID": alice_workspace})
        assert response.status_code == 404
        assert bob_state["workspace"]["id"] != alice_workspace
        # Without the pin, Bob resolves to his own workspace normally.
        assert bob.get("/session").json()["workspace"]["id"] == bob_state["workspace"]["id"]
        # And the strict API path still refuses it outright.
        assert bob.get("/accounts", headers={"X-Workspace-ID": alice_workspace}).status_code == 404
        # Selecting a workspace he does not own is rejected, so it can never be persisted.
        assert bob.post(
            f"/workspaces/{alice_workspace}/select", headers=ORIGIN
        ).status_code == 404


def test_signed_in_user_with_no_workspace_is_sent_back_to_sign_in(settings):
    """Defensive: an orphaned session (e.g. a restored/partial database) must not 404."""
    app = create_app(settings)
    with TestClient(app) as client:
        signup(client)
        with app.state.store.connection() as db:
            # Simulate a database restored without its workspaces. Foreign keys are
            # disabled only to create that inconsistent state for the test.
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("DELETE FROM workspaces")
        # The shell and bootstrap must not 404; they report no usable session.
        assert client.get("/app", follow_redirects=False).status_code == 303
        assert client.get("/app", follow_redirects=False).headers["location"] == "/login"
        current = client.get("/session")
        assert current.status_code == 200
        assert current.json()["authenticated"] is False
        assert current.json()["workspace"] is None
        assert client.get("/login", follow_redirects=False).status_code == 200


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


def test_post_history_is_keyed_by_account(settings):
    store = Store(settings.database_path, settings.encryption_key)
    tokens = {"access_token": "token", "expires_in": 3600}
    first = store.save_account("youtube", "channel", "First", tokens, "scope")
    scope = store.workspace.set("studio")
    try:
        second = store.save_account("youtube", "channel", "Second", tokens, "scope")
    finally:
        store.workspace.reset(scope)
    store.save_youtube_history(
        first["id"], "video-1", "First upload", "", "a.mp4", "video/mp4", None,
        "https://youtu.be/video-1", "private",
    )
    store.save_youtube_history(
        second["id"], "video-1", "Second upload", "", "b.mp4", "video/mp4", None,
        "https://youtu.be/video-1", "private",
    )
    # Each workspace sees only its own channel's receipt for the same platform id.
    assert [row["title"] for row in store.list_post_history()] == ["First upload"]
    scope = store.workspace.set("studio")
    try:
        assert [row["title"] for row in store.list_post_history()] == ["Second upload"]
    finally:
        store.workspace.reset(scope)


def test_legacy_post_history_key_is_migrated_without_losing_rows(settings):
    # Build a database the way older releases shipped it: history keyed by platform post.
    with sqlite3.connect(settings.database_path) as db:
        db.executescript("""
            CREATE TABLE accounts (
                id TEXT PRIMARY KEY, provider TEXT NOT NULL, external_id TEXT NOT NULL,
                name TEXT NOT NULL, access_token TEXT NOT NULL, refresh_token TEXT,
                expires_at INTEGER NOT NULL, scopes TEXT NOT NULL,
                token_updated_at INTEGER NOT NULL, created_at INTEGER NOT NULL,
                avatar_url TEXT, workspace_id TEXT NOT NULL DEFAULT 'legacy',
                UNIQUE(workspace_id, provider, external_id)
            );
            INSERT INTO accounts VALUES ('acct', 'youtube', 'remote', 'Channel',
                'encrypted', NULL, 9999999999, 'scope', 1, 1, NULL, 'legacy');
            CREATE TABLE post_history (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL CHECK(provider IN ('youtube', 'instagram')),
                account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                platform_post_id TEXT NOT NULL,
                title TEXT, caption TEXT NOT NULL DEFAULT '',
                media_name TEXT, media_type TEXT, media_url TEXT,
                post_url TEXT, visibility TEXT,
                published_at INTEGER NOT NULL,
                UNIQUE(provider, platform_post_id)
            );
            INSERT INTO post_history VALUES ('youtube-video-1', 'youtube', 'acct', 'video-1',
                'Existing upload', 'desc', 'a.mp4', 'video/mp4', NULL,
                'https://youtu.be/video-1', 'private', 100);
            INSERT INTO post_history VALUES ('instagram-media-1', 'instagram', 'acct', 'media-1',
                NULL, 'caption', NULL, 'IMAGE', NULL, NULL, NULL, 200);
        """)
    migrated = Store(settings.database_path, settings.encryption_key)
    rows = migrated.list_post_history()
    assert {row["id"] for row in rows} == {"youtube-video-1", "instagram-media-1"}
    with migrated.connection() as db:
        assert list(db.execute("PRAGMA foreign_key_check")) == []
        keys = {
            tuple(row[2] for row in db.execute(f'PRAGMA index_info("{index[1]}")'))
            for index in db.execute("PRAGMA index_list(post_history)").fetchall()
            if index[2]
        }
        assert ("provider", "account_id", "platform_post_id") in keys
        assert ("provider", "platform_post_id") not in keys
    # Re-opening an already-migrated database must be a no-op, not a second rebuild.
    again = Store(settings.database_path, settings.encryption_key)
    assert {row["id"] for row in again.list_post_history()} == {
        "youtube-video-1", "instagram-media-1",
    }
    # Deleting the account still cascades its history after the rebuild.
    assert again.delete_account("acct")
    assert again.list_post_history() == []


def test_account_sessions_and_workspaces_survive_restart(settings):
    with TestClient(create_app(settings)) as client:
        original = signup(client)
        token = client.cookies.get("relay_session")
    with TestClient(create_app(settings)) as restarted:
        restarted.cookies.set("relay_session", token)
        current = restarted.get("/session").json()
        assert current["user"] == original["user"]
        assert current["workspaces"] == original["workspaces"]
