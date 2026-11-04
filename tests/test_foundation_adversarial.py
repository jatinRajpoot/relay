"""Independent adversarial regressions for the foundation repair round.

These tests deliberately target the seams the implementation report claims to have
closed, using states the shipped suite does not build: a mixed healthy/unreadable
workspace, an unreadable *refresh* token, redirect and malformed-2xx publish replies,
a cross-tenant refresh probe, a real concurrent refresh, and a history rebuild that
fails part way through.

They assert observable HTTP/DB behaviour, not internal structure.
"""

import asyncio
import sqlite3
import time

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from relay.app import create_app
from relay.config import Settings
from relay.store import Store

KEY = "test-service-key-with-at-least-32-characters"
HEADERS = {"X-API-Key": KEY}


@pytest.fixture
def app_settings(tmp_path):
    return Settings(
        api_key=KEY,
        encryption_key=Fernet.generate_key().decode(),
        youtube_client_id="test-google-client",
        youtube_client_secret="test-google-secret",
        instagram_app_id="test-ig-client",
        instagram_app_secret="test-ig-secret",
        database_path=tmp_path / "tokens.sqlite3",
    )


def youtube_channels(request):
    """Answer the channel lookup for any id, with an avatar, so avatars can be asserted."""
    channel_id = request.url.params.get("id") or "chan-1"
    return httpx.Response(
        200,
        json={
            "items": [
                {
                    "id": channel_id,
                    "snippet": {
                        "title": channel_id,
                        "thumbnails": {"default": {"url": f"https://img.test/{channel_id}.png"}},
                    },
                }
            ]
        },
    )


def seed_youtube(store, external_id, name, access="good-access", refresh="good-refresh"):
    return store.save_account(
        "youtube",
        external_id,
        name,
        {"access_token": access, "refresh_token": refresh, "expires_in": 3600},
        "scope",
    )


def break_token(store, account_id, field="access_token"):
    with store.connection() as db:
        db.execute(f"UPDATE accounts SET {field}='encrypted-with-another-key' WHERE id=?",
                   (account_id,))


def test_unreadable_account_does_not_hide_a_healthy_one(settings):
    """App-boot listing must return 200 and still fetch the healthy channel's avatar."""
    app = create_app(settings, httpx.MockTransport(youtube_channels))
    with TestClient(app) as client:
        store = app.state.store
        healthy = seed_youtube(store, "chan-1", "Healthy")
        broken = seed_youtube(store, "chan-2", "Broken")
        break_token(store, broken["id"])

        response = client.get("/accounts?include_avatars=true", headers=HEADERS)

        assert response.status_code == 200
        rows = {row["external_id"]: row for row in response.json()["accounts"]}
        assert set(rows) == {"chan-1", "chan-2"}
        assert rows["chan-1"]["avatar_url"] == "https://img.test/chan-1.png"
        # The unreadable row is isolated: listed, but with no avatar and no token material.
        assert rows["chan-2"]["avatar_url"] is None
        assert "access_token" not in rows["chan-2"]
        assert "encrypted-with-another-key" not in response.text
        assert healthy["id"] != broken["id"]


def test_unreadable_refresh_token_is_a_409_not_a_500(settings):
    """The helper must cover refresh_token too, not only access_token."""
    app = create_app(settings, httpx.MockTransport(youtube_channels))
    with TestClient(app) as client:
        store = app.state.store
        account = seed_youtube(store, "chan-1", "Channel")
        break_token(store, account["id"], "refresh_token")
        with store.connection() as db:
            db.execute("UPDATE accounts SET expires_at=? WHERE id=?",
                       (int(time.time()) - 1, account["id"]))

        response = client.post(f"/accounts/{account['id']}/refresh", headers=HEADERS)

        assert response.status_code == 409
        assert "Reconnect" in response.json()["detail"]
        # A local credential failure must not rewrite the stored row.
        stored = store.get_account(account["id"])
        assert stored["refresh_token"] == "encrypted-with-another-key"


def test_cross_tenant_refresh_probe_allocates_no_lock(settings):
    """A real account id from another workspace is a 404 and must not create a lock entry."""
    app = create_app(settings, httpx.MockTransport(youtube_channels))
    with TestClient(app) as client:
        store = app.state.store
        scope = store.workspace.set("someone-elses-workspace")
        try:
            foreign = seed_youtube(store, "chan-1", "Other tenant")
        finally:
            store.workspace.reset(scope)
        # The API-key session resolves to the legacy workspace, where this id does not exist.
        assert store.get_account(foreign["id"]) is None

        response = client.post(f"/accounts/{foreign['id']}/refresh", headers=HEADERS)

        assert response.status_code == 404
        assert foreign["id"] not in app.state.refresh_locks


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(302, headers={"location": "https://graph.instagram.com/elsewhere"}),
        httpx.Response(200, content=b"<html>not json</html>"),
        httpx.Response(200, json={"ok": True}),
    ],
    ids=["redirect", "malformed-2xx", "missing-id"],
)
def test_unreadable_publish_reply_stays_ambiguous(settings, reply):
    """A reply we cannot interpret may still have published, so it must block retry."""
    calls = []

    def handler(request):
        if request.url.path.endswith("/media_publish"):
            calls.append(request)
            return reply
        if request.url.path == "/v25.0/container-1":
            return httpx.Response(200, json={"status_code": "FINISHED"})
        if request.url.path == "/v25.0/prof":
            return httpx.Response(200, json={"user_id": "prof", "username": "ig"})
        raise AssertionError(request.url)

    app = create_app(settings, httpx.MockTransport(handler))
    with TestClient(app) as client:
        store = app.state.store
        account = store.save_account(
            "instagram", "prof", "IG",
            {"access_token": "ig-token", "expires_in": 5184000}, "scope",
        )
        post = store.create_post(account["id"], "container-1")
        with store.connection() as db:
            db.execute("UPDATE instagram_posts SET status='ready' WHERE id=?", (post["id"],))

        first = client.post(f"/instagram/posts/{post['id']}/publish", headers=HEADERS)

        assert first.status_code == 502
        assert store.get_post(post["id"])["status"] == "unknown"
        # No hidden retry: a second publish is refused and never reaches the provider.
        assert client.post(f"/instagram/posts/{post['id']}/publish", headers=HEADERS).status_code == 409
        assert len(calls) == 1


def test_concurrent_refresh_calls_are_serialized(settings):
    """Two simultaneous refreshes for one account must never overlap at the provider."""
    app = create_app(settings, httpx.MockTransport(youtube_channels))
    in_flight = 0
    peak = 0
    calls = []

    async def scenario():
        nonlocal in_flight, peak
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                store = app.state.store
                account = seed_youtube(store, "chan-1", "Channel")
                with store.connection() as db:
                    db.execute("UPDATE accounts SET expires_at=? WHERE id=?",
                               (int(time.time()) - 1, account["id"]))

                entered = asyncio.Event()
                release = asyncio.Event()

                async def slow_refresh(provider, access_token, refresh_token):
                    nonlocal in_flight, peak
                    calls.append(access_token)
                    in_flight += 1
                    peak = max(peak, in_flight)
                    entered.set()
                    try:
                        await release.wait()
                        return {"access_token": "renewed", "expires_in": 3600}
                    finally:
                        in_flight -= 1

                app.state.providers.refresh = slow_refresh
                first = asyncio.create_task(
                    client.post(f"/accounts/{account['id']}/refresh", headers=HEADERS)
                )
                await asyncio.wait_for(entered.wait(), 5)
                second = asyncio.create_task(
                    client.post(f"/accounts/{account['id']}/refresh", headers=HEADERS)
                )
                await asyncio.sleep(0.2)
                release.set()
                return await asyncio.wait_for(first, 5), await asyncio.wait_for(second, 5)

    first, second = asyncio.run(scenario())

    # The lock is doing its job: the provider never sees two refreshes at once.
    assert peak == 1
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["account"]["id"] == second.json()["account"]["id"]
    # Note for the report: /refresh passes force=True, so a second click is a second
    # provider call (serialized, not deduplicated). Asserted here so a future change
    # that deduplicates is visible rather than silent.
    assert len(calls) == 2


def test_failed_history_rebuild_leaves_the_original_table_intact(settings):
    """A rebuild that cannot copy a row must roll back, not half-migrate the database."""
    path = settings.database_path
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE accounts (
                id TEXT PRIMARY KEY, provider TEXT NOT NULL, external_id TEXT NOT NULL,
                name TEXT NOT NULL, access_token TEXT NOT NULL, refresh_token TEXT,
                expires_at INTEGER NOT NULL, scopes TEXT NOT NULL,
                token_updated_at INTEGER NOT NULL, created_at INTEGER NOT NULL,
                UNIQUE(provider, external_id)
            );
            INSERT INTO accounts VALUES ('acct', 'youtube', 'remote', 'Channel',
                'encrypted', NULL, 9999999999, 'scope', 1, 1);
            CREATE TABLE post_history (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL CHECK(provider IN ('youtube', 'instagram')),
                account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                platform_post_id TEXT NOT NULL, title TEXT,
                caption TEXT NOT NULL DEFAULT '', media_name TEXT, media_type TEXT,
                media_url TEXT, post_url TEXT, visibility TEXT,
                published_at INTEGER NOT NULL,
                UNIQUE(provider, platform_post_id)
            );
            INSERT INTO post_history VALUES ('keep', 'youtube', 'acct', 'video-1', 'Keep me',
                '', NULL, NULL, NULL, NULL, NULL, 100);
        """)
        # A row the rebuilt schema's CHECK would reject. Written with constraints off so
        # the copy step fails after the new table has been created.
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute("INSERT INTO post_history VALUES ('bad', 'tiktok', 'acct', 'video-2', 'Bad',"
                   " '', NULL, NULL, NULL, NULL, NULL, 200)")
        db.commit()

    with pytest.raises(sqlite3.IntegrityError):
        Store(path, settings.encryption_key)

    with sqlite3.connect(path) as db:
        rows = list(db.execute("SELECT id FROM post_history ORDER BY id"))
        keys = {
            tuple(row[2] for row in db.execute(f'PRAGMA index_info("{index[1]}")'))
            for index in db.execute("PRAGMA index_list(post_history)").fetchall()
            if index[2]
        }
        leftover = db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='post_history_scoped'"
        ).fetchone()
    # The original rows and the original unique key survive, so a later retry can succeed.
    assert [row[0] for row in rows] == ["bad", "keep"]
    assert ("provider", "platform_post_id") in keys
    assert ("provider", "account_id", "platform_post_id") not in keys
    assert leftover is None
