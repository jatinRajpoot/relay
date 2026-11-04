"""Durable publishing receipts, recovery reads, and idempotent dispatch.

These tests exercise the contract the frontend depends on:

* a receipt exists before any external side-effecting call;
* a repeated idempotency key answers from the receipt and never re-dispatches;
* reads and lists never call a provider;
* a stale claim becomes ``unknown`` without a second publish;
* every read is workspace-scoped and free of credentials.
"""

from __future__ import annotations

import sqlite3
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from test_frontend import ORIGIN
from test_service import HEADERS, instagram_provider, youtube_provider

from relay.app import create_app
from relay.store import Store

PASSWORD = "a long test password"
MEDIA = "https://cdn.example/media"


def signup(client, email="alex@example.com"):
    result = client.post(
        "/auth/signup",
        headers=ORIGIN,
        json={
            "email": email,
            "password": PASSWORD,
            "name": "Alex",
            "workspace_name": "Studio",
        },
    )
    assert result.status_code == 201, result.text
    return client.get("/session").json()


def connect(client, provider="instagram"):
    start = client.post(f"/oauth/{provider}/authorize", headers=ORIGIN)
    assert start.status_code == 200, start.text
    state = parse_qs(urlsplit(start.json()["authorization_url"]).query)["state"][0]
    response = client.get(
        f"/oauth/{provider}/callback", params={"state": state, "code": "test-code"}
    )
    assert response.status_code == 200, response.text
    return response.json()["account"]


class Instagram:
    """A controllable Instagram provider double with call counters."""

    def __init__(self):
        self.create_calls = 0
        self.publish_calls = 0
        self.status_code = "FINISHED"
        self.create_response = None
        self.publish_response = None
        self.on_create = None

    def __call__(self, request):
        path = request.url.path
        if path == "/v25.0/professional-id/media":
            self.create_calls += 1
            if self.on_create:
                self.on_create()
            if self.create_response is not None:
                return self.create_response
            return httpx.Response(200, json={"id": f"container-{self.create_calls}"})
        if path.endswith("/media_publish"):
            self.publish_calls += 1
            if self.publish_response is not None:
                return self.publish_response
            return httpx.Response(200, json={"id": "published-1"})
        if path.startswith("/v25.0/container"):
            return httpx.Response(200, json={"status_code": self.status_code})
        return instagram_provider(request)


class YouTube:
    def __init__(self):
        self.uploads = 0
        self.upload_response = None
        self.on_upload = None

    def __call__(self, request):
        if request.url.path == "/upload/youtube/v3/videos":
            if request.method == "POST":
                return httpx.Response(
                    200,
                    headers={
                        "location": "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=x"
                    },
                )
            self.uploads += 1
            if self.on_upload:
                self.on_upload()
            if self.upload_response is not None:
                return self.upload_response
            return httpx.Response(201, json={"id": "video-1", "status": {"privacyStatus": "private"}})
        return youtube_provider(request)


def make_app(settings, handler):
    return create_app(settings, httpx.MockTransport(handler))


def publish_ready(client, post_id, key=None):
    headers = dict(ORIGIN)
    if key:
        headers["Idempotency-Key"] = key
    return client.post(f"/instagram/posts/{post_id}/publish", headers=headers)


def create_instagram_post(client, account, key=None, caption="Hello"):
    headers = dict(ORIGIN)
    if key:
        headers["Idempotency-Key"] = key
    return client.post(
        "/instagram/posts",
        headers=headers,
        json={
            "account_id": account["id"],
            "media_type": "IMAGE",
            "media_url": MEDIA,
            "caption": caption,
        },
    )


def upload_youtube(client, account, key=None, title="Test upload", content=b"video-data"):
    headers = dict(ORIGIN)
    if key:
        headers["Idempotency-Key"] = key
    return client.post(
        "/youtube/videos",
        headers=headers,
        data={"account_id": account["id"], "title": title},
        files={"file": ("video.mp4", content, "video/mp4")},
    )


def attempts(client, **params):
    headers = params.pop("headers", None)
    response = client.get("/publishing/attempts", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


# ── Receipts exist before the external call ──────────────────────────────


def test_instagram_receipt_exists_before_the_container_call(settings):
    provider = Instagram()
    app = make_app(settings, provider)
    seen = []

    def inspect():
        # Runs *inside* the provider call: the receipt must already be durable.
        with sqlite3.connect(settings.database_path) as db:
            seen.extend(db.execute("SELECT status FROM publishing_attempts").fetchall())

    provider.on_create = inspect
    with TestClient(app) as client:
        signup(client)
        account = connect(client)
        response = create_instagram_post(client, account)
        assert response.status_code == 201, response.text
        assert seen == [("submitted",)]
        rows = attempts(client)["attempts"]
        assert [row["status"] for row in rows] == ["processing"]
        assert rows[0]["provider"] == "instagram"
        assert rows[0]["account_id"] == account["id"]
        assert rows[0]["post_id"] == response.json()["id"]
        assert rows[0]["provider_ref"] == "container-1"


def test_youtube_receipt_exists_before_the_upload_starts(settings):
    provider = YouTube()
    app = make_app(settings, provider)
    seen = []

    def inspect():
        with sqlite3.connect(settings.database_path) as db:
            seen.extend(db.execute("SELECT status FROM publishing_attempts").fetchall())

    provider.on_upload = inspect
    with TestClient(app) as client:
        signup(client)
        account = connect(client, "youtube")
        response = upload_youtube(client, account)
        assert response.status_code == 201, response.text
        assert seen == [("submitted",)]
        rows = attempts(client, status="all")["attempts"]
        assert rows[0]["status"] == "published"
        assert rows[0]["provider_ref"] == "video-1"
        assert rows[0]["provider_url"] == "https://www.youtube.com/watch?v=video-1"


# ── Idempotency ──────────────────────────────────────────────────────────


def test_repeated_create_key_returns_the_same_job_without_a_second_call(settings):
    provider = Instagram()
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        first = create_instagram_post(client, account, key="post-key-0001")
        second = create_instagram_post(client, account, key="post-key-0001")
        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json() == second.json()
        assert provider.create_calls == 1
        assert len(attempts(client, status="all")["attempts"]) == 1


def test_reused_key_with_a_different_payload_is_refused(settings):
    provider = Instagram()
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        assert create_instagram_post(client, account, key="post-key-0002").status_code == 201
        changed = create_instagram_post(client, account, key="post-key-0002", caption="Different")
        assert changed.status_code == 409
        assert "different post" in changed.json()["detail"]
        assert provider.create_calls == 1


def test_body_and_header_key_conflict_is_rejected(settings):
    provider = Instagram()
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        response = client.post(
            "/instagram/posts",
            headers=ORIGIN | {"Idempotency-Key": "header-key-01"},
            json={
                "account_id": account["id"],
                "media_type": "IMAGE",
                "media_url": MEDIA,
                "idempotency_key": "body-key-0001",
            },
        )
        assert response.status_code == 422
        assert provider.create_calls == 0


def test_repeated_publish_key_does_not_publish_twice(settings):
    provider = Instagram()
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        first = publish_ready(client, post["id"], key="publish-key-01")
        second = publish_ready(client, post["id"], key="publish-key-01")
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json() == second.json()
        assert provider.publish_calls == 1


def test_repeated_youtube_key_returns_the_existing_receipt(settings):
    provider = YouTube()
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client, "youtube")
        first = upload_youtube(client, account, key="upload-key-01")
        second = upload_youtube(client, account, key="upload-key-01")
        assert first.status_code == 201
        assert second.status_code == 201
        assert second.json()["id"] == "video-1"
        assert second.json()["attempt"]["id"] == first.json()["attempt"]["id"]
        assert second.json()["attempt"]["status"] == "published"
        assert provider.uploads == 1


def test_youtube_key_reuse_with_a_different_title_is_refused(settings):
    provider = YouTube()
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client, "youtube")
        assert upload_youtube(client, account, key="upload-key-02").status_code == 201
        changed = upload_youtube(client, account, key="upload-key-02", title="Other title")
        assert changed.status_code == 409
        assert provider.uploads == 1


# ── Failure classification ───────────────────────────────────────────────


def test_definite_container_rejection_is_a_failed_receipt(settings):
    provider = Instagram()
    provider.create_response = httpx.Response(400, json={"error": {"message": "bad media"}})
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        response = create_instagram_post(client, account)
        assert response.status_code == 502
        rows = attempts(client)["attempts"]
        assert rows[0]["status"] == "failed"
        assert rows[0]["next_action"] == "retry"
        assert "bad media" not in response.text


def test_ambiguous_container_failure_is_unknown_and_verifiable(settings):
    provider = Instagram()
    provider.create_response = httpx.Response(503, json={"error": {"message": "upstream"}})
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        assert create_instagram_post(client, account).status_code == 502
        rows = attempts(client)["attempts"]
        assert rows[0]["status"] == "unknown"
        assert rows[0]["next_action"] == "verify"
        assert rows[0]["verification_url"] == "https://www.instagram.com"


def test_transport_failure_during_upload_is_unknown_with_a_studio_link(settings):
    provider = YouTube()

    def timeout():
        raise httpx.ReadTimeout("secret-provider-url", request=None)

    provider.on_upload = timeout
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client, "youtube")
        response = upload_youtube(client, account)
        assert response.status_code == 502
        assert "secret-provider-url" not in response.text
        rows = attempts(client)["attempts"]
        assert rows[0]["status"] == "unknown"
        assert rows[0]["verification_url"] == "https://studio.youtube.com"


def test_definite_upload_rejection_is_failed_and_safe_to_retry(settings):
    provider = YouTube()
    provider.upload_response = httpx.Response(400, json={"error": {"message": "invalid"}})
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client, "youtube")
        assert upload_youtube(client, account).status_code == 502
        rows = attempts(client)["attempts"]
        assert rows[0]["status"] == "failed"
        assert rows[0]["next_action"] == "retry"


# ── Recovery across restart ──────────────────────────────────────────────


def test_receipts_survive_an_application_recreate(settings):
    provider = Instagram()
    app = make_app(settings, provider)
    with TestClient(app) as client:
        signup(client)
        account = connect(client)
        created = create_instagram_post(client, account).json()
        token = client.cookies.get("relay_session")
    with TestClient(make_app(settings, provider)) as restarted:
        restarted.cookies.set("relay_session", token)
        rows = attempts(restarted)["attempts"]
        assert [row["post_id"] for row in rows] == [created["id"]]
        assert rows[0]["status"] == "processing"
        assert rows[0]["next_action"] == "check"


def test_ready_and_unknown_jobs_reload_with_their_own_state(settings):
    provider = Instagram()
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        ready = create_instagram_post(client, account).json()
        unknown = create_instagram_post(client, account).json()
        # The first container finished processing; the second publish was interrupted.
        client.get(f"/instagram/posts/{ready['id']}")
        with client.app.state.store.connection() as db:
            db.execute("UPDATE instagram_posts SET status='unknown' WHERE id=?", (unknown["id"],))
            db.execute(
                "UPDATE publishing_attempts SET status='unknown' WHERE post_id=?", (unknown["id"],)
            )
    with TestClient(make_app(settings, provider)) as restarted:
        restarted.cookies.set("relay_session", client.cookies.get("relay_session"))
        rows = {row["post_id"]: row for row in attempts(restarted)["attempts"]}
        assert rows[ready["id"]]["status"] == "ready"
        assert rows[ready["id"]]["next_action"] == "publish"
        assert rows[unknown["id"]]["status"] == "unknown"
        assert rows[unknown["id"]]["next_action"] == "verify"


# ── Stale claims: no re-dispatch ─────────────────────────────────────────


def test_stale_publish_claim_becomes_unknown_without_resending(settings):
    provider = Instagram()
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        # Simulate a publish whose claim was taken and whose response was lost.
        with client.app.state.store.connection() as db:
            db.execute("UPDATE instagram_posts SET status='publishing' WHERE id=?", (post["id"],))
            db.execute(
                "UPDATE publishing_attempts SET status='submitted', claimed_at=? WHERE post_id=?",
                (int(time.time()) - 3600, post["id"]),
            )
        response = client.get(f"/instagram/posts/{post['id']}")
        assert response.status_code == 200
        assert response.json()["status"] == "unknown"
        assert provider.publish_calls == 0
        rows = attempts(client)["attempts"]
        assert rows[0]["status"] == "unknown"
        assert rows[0]["provider_ref"] == "container-1"


def test_listing_never_dispatches_a_provider_call(settings):
    provider = Instagram()
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        before = (provider.create_calls, provider.publish_calls)
        for _ in range(3):
            attempts(client, status="all")
            client.get(f"/publishing/attempts/{attempts(client)['attempts'][0]['id']}")
        assert (provider.create_calls, provider.publish_calls) == before
        assert client.get(f"/instagram/posts/{post['id']}").status_code == 200
        assert provider.publish_calls == 0


def test_an_unknown_attempt_cannot_be_republished_through_its_job(settings):
    provider = Instagram()
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        with client.app.state.store.connection() as db:
            db.execute("UPDATE instagram_posts SET status='unknown' WHERE id=?", (post["id"],))
            db.execute(
                "UPDATE publishing_attempts SET status='unknown' WHERE post_id=?", (post["id"],)
            )
        assert publish_ready(client, post["id"]).status_code == 409
        assert provider.publish_calls == 0


# ── Pagination ───────────────────────────────────────────────────────────


def test_equal_timestamps_paginate_without_duplicates_or_gaps(settings):
    store = Store(settings.database_path, settings.encryption_key)
    account = store.save_account(
        "youtube", "channel", "Channel", {"access_token": "t", "expires_in": 3600}, "scope"
    )
    stamp = 1_700_000_000
    ids = []
    for index in range(5):
        row = store.create_attempt(
            account_id=account["id"],
            provider="youtube",
            status="failed",
            title=f"Video {index}",
            created_at=stamp,
            attempt_id=f"attempt-{index}",
        )
        ids.append(row["id"])
    app = make_app(settings, youtube_provider)
    with TestClient(app) as client:
        seen, cursor, pages = [], None, 0
        while True:
            params = {"status": "all", "limit": 2, "headers": HEADERS}
            if cursor:
                params["cursor"] = cursor
            page = attempts(client, **params)
            seen.extend(row["id"] for row in page["attempts"])
            pages += 1
            cursor = page["next_cursor"]
            if not cursor:
                break
            assert pages < 10
        assert seen == sorted(ids, reverse=True)
        assert len(seen) == len(set(seen)) == 5


@pytest.mark.parametrize("cursor", ["not-a-cursor", "!!!!", "MTIz", "e30", "x" * 600])
def test_malformed_cursors_are_rejected(settings, cursor):
    with TestClient(make_app(settings, youtube_provider)) as client:
        response = client.get("/publishing/attempts", params={"cursor": cursor}, headers=HEADERS)
        assert response.status_code == 422
        assert response.json()["detail"] == "Invalid cursor."


@pytest.mark.parametrize("limit", [0, -1, 101, 1000])
def test_out_of_range_limits_are_rejected(settings, limit):
    with TestClient(make_app(settings, youtube_provider)) as client:
        assert client.get(
            "/publishing/attempts", params={"limit": limit}, headers=HEADERS
        ).status_code == 422


def test_active_filter_excludes_confirmed_and_expired(settings):
    store = Store(settings.database_path, settings.encryption_key)
    account = store.save_account(
        "youtube", "channel", "Channel", {"access_token": "t", "expires_in": 3600}, "scope"
    )
    for status in ("published", "expired", "failed", "unknown", "processing"):
        store.create_attempt(account_id=account["id"], provider="youtube", status=status)
    with TestClient(make_app(settings, youtube_provider)) as client:
        active = {row["status"] for row in attempts(client, headers=HEADERS)["attempts"]}
        assert active == {"failed", "unknown", "processing"}
        everything = {
            row["status"] for row in attempts(client, status="all", headers=HEADERS)["attempts"]
        }
        assert everything == {"published", "expired", "failed", "unknown", "processing"}


# ── Workspace isolation and secret hygiene ───────────────────────────────


def test_attempts_are_isolated_per_workspace(settings):
    provider = Instagram()
    app = make_app(settings, provider)
    with TestClient(app) as alice, TestClient(app) as bob:
        signup(alice)
        alice_account = connect(alice)
        alice_post = create_instagram_post(alice, alice_account).json()
        alice_attempt = attempts(alice)["attempts"][0]["id"]

        signup(bob, "bob@example.com")
        assert attempts(bob)["attempts"] == []
        assert bob.get(f"/publishing/attempts/{alice_attempt}").status_code == 404
        # A pin naming Alice's workspace is refused outright.
        alice_workspace = alice.get("/session").json()["workspace"]["id"]
        assert bob.get(
            "/publishing/attempts", headers={"X-Workspace-ID": alice_workspace}
        ).status_code == 404
        # Alice still sees her own job.
        assert [row["post_id"] for row in attempts(alice)["attempts"]] == [alice_post["id"]]


def test_attempt_responses_never_contain_credentials(settings):
    provider = Instagram()
    app = make_app(settings, provider)
    with TestClient(app) as client:
        signup(client)
        account = connect(client)
        create_instagram_post(client, account)
        store = app.state.store
        scope = store.workspace.set(client.get("/session").json()["workspace"]["id"])
        try:
            raw = store.get_account(account["id"])
        finally:
            store.workspace.reset(scope)
        listing = client.get("/publishing/attempts", params={"status": "all"}).text
        detail = client.get(
            f"/publishing/attempts/{attempts(client, status='all')['attempts'][0]['id']}"
        ).text
        secrets = ["ig-long", raw["access_token"], "Bearer", "access_token"]
        if raw["refresh_token"]:
            secrets.append(raw["refresh_token"])
        for secret in secrets:
            assert secret not in listing
            assert secret not in detail


def test_attempts_are_deleted_with_their_account(settings):
    provider = Instagram()
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        create_instagram_post(client, account)
        assert attempts(client)["attempts"]
        assert client.delete(f"/accounts/{account['id']}", headers=ORIGIN).status_code == 204
        assert attempts(client)["attempts"] == []


# ── Partial multi-destination outcomes ───────────────────────────────────


def test_partial_outcomes_are_retained_independently(settings):
    instagram = Instagram()
    youtube = YouTube()

    def handler(request):
        if request.url.host in {"graph.instagram.com", "api.instagram.com"} or request.url.path.startswith(
            "/v25.0"
        ):
            return instagram(request)
        return youtube(request)

    with TestClient(make_app(settings, handler)) as client:
        signup(client)
        ig = connect(client, "instagram")
        yt = connect(client, "youtube")
        assert create_instagram_post(client, ig).status_code == 201
        assert upload_youtube(client, yt).status_code == 201
        rows = {row["provider"]: row for row in attempts(client, status="all")["attempts"]}
        assert rows["instagram"]["status"] == "processing"
        assert rows["youtube"]["status"] == "published"
        # The YouTube success is not hidden by the Instagram work still in flight.
        assert len(attempts(client, status="all")["attempts"]) == 2


# ── Migration ────────────────────────────────────────────────────────────


def test_legacy_jobs_and_history_are_backfilled_idempotently(settings):
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
            INSERT INTO accounts VALUES ('acct', 'instagram', 'remote', 'Channel',
                'encrypted', NULL, 9999999999, 'scope', 1, 1, NULL, 'legacy');
            CREATE TABLE instagram_posts (
                id TEXT PRIMARY KEY, account_id TEXT REFERENCES accounts(id) ON DELETE CASCADE,
                container_id TEXT NOT NULL UNIQUE, media_id TEXT, status TEXT, created_at INTEGER
            );
            INSERT INTO instagram_posts VALUES ('ready-job', 'acct', 'c1', NULL, 'ready', 10);
            INSERT INTO instagram_posts VALUES ('stuck-job', 'acct', 'c2', NULL, 'publishing', 20);
            INSERT INTO instagram_posts VALUES ('done-job', 'acct', 'c3', 'm3', 'published', 30);
            CREATE TABLE post_history (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL CHECK(provider IN ('youtube', 'instagram')),
                account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                platform_post_id TEXT NOT NULL,
                title TEXT, caption TEXT NOT NULL DEFAULT '',
                media_name TEXT, media_type TEXT, media_url TEXT,
                post_url TEXT, visibility TEXT,
                published_at INTEGER NOT NULL,
                UNIQUE(provider, account_id, platform_post_id)
            );
            INSERT INTO post_history VALUES ('instagram-m3', 'instagram', 'acct', 'm3',
                NULL, '', NULL, 'IMAGE', NULL, NULL, NULL, 30);
            INSERT INTO post_history VALUES ('youtube-v9', 'youtube', 'acct', 'v9',
                'Old upload', '', 'a.mp4', 'video/mp4', NULL,
                'https://youtu.be/v9', 'private', 40);
        """)
    store = Store(settings.database_path, settings.encryption_key)
    with store.connection() as db:
        rows = {
            row["post_id"]: row
            for row in db.execute("SELECT * FROM publishing_attempts WHERE post_id IS NOT NULL")
        }
        history = {
            row["provider_ref"]: row
            for row in db.execute("SELECT * FROM publishing_attempts WHERE post_id IS NULL")
        }
    assert rows["ready-job"]["status"] == "ready"
    assert rows["stuck-job"]["status"] == "unknown"
    assert rows["stuck-job"]["error"]
    assert rows["done-job"]["status"] == "published"
    # A published job's history row is recognised as already represented, so the
    # backfill does not create a second receipt for the same media id. History with
    # no surviving job (an older YouTube upload) does get its own receipt.
    assert list(history) == ["v9"]
    assert history["v9"]["status"] == "published"
    assert history["v9"]["title"] == "Old upload"
    with store.connection() as db:
        assert list(db.execute("PRAGMA foreign_key_check")) == []

    # Re-opening must not duplicate receipts.
    again = Store(settings.database_path, settings.encryption_key)
    with again.connection() as db:
        total = db.execute("SELECT COUNT(*) FROM publishing_attempts").fetchone()[0]
    assert total == 4
    # Deleting the account still cascades its receipts.
    assert again.delete_account("acct")
    with again.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM publishing_attempts").fetchone()[0] == 0


def test_backfill_does_not_invent_failures_or_drafts(settings):
    store = Store(settings.database_path, settings.encryption_key)
    account = store.save_account(
        "instagram", "remote", "Channel", {"access_token": "t", "expires_in": 3600}, "scope"
    )
    store.create_post(account["id"], "container-x")
    reopened = Store(settings.database_path, settings.encryption_key)
    with reopened.connection() as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM publishing_attempts")]
    assert [row["status"] for row in rows] == ["processing"]
    assert rows[0]["error"] is None


# ── Receipt persistence failures and malformed provider replies ──────────


def test_unwritable_receipt_blocks_the_publish_before_dispatch(settings):
    """If the durable claim cannot be written, no external call may be made."""
    provider = Instagram()
    app = make_app(settings, provider)
    with TestClient(app) as client:
        signup(client)
        account = connect(client)
        store = app.state.store

        def refuse(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        original = store.create_attempt
        store.create_attempt = refuse
        try:
            response = create_instagram_post(client, account)
        finally:
            store.create_attempt = original
        assert response.status_code >= 500
        assert provider.create_calls == 0


def test_unwritable_claim_blocks_the_upload_before_dispatch(settings):
    provider = YouTube()
    app = make_app(settings, provider)
    with TestClient(app) as client:
        signup(client)
        account = connect(client, "youtube")
        store = app.state.store

        def refuse(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        original = store.create_attempt
        store.create_attempt = refuse
        try:
            response = upload_youtube(client, account)
        finally:
            store.create_attempt = original
        assert response.status_code >= 500
        assert provider.uploads == 0


def test_unreadable_success_reply_is_recorded_as_unknown(settings):
    provider = Instagram()
    provider.create_response = httpx.Response(200, content=b"not json")
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        assert create_instagram_post(client, account).status_code == 502
        rows = attempts(client)["attempts"]
        assert rows[0]["status"] == "unknown"
        assert rows[0]["next_action"] == "verify"


def test_a_failed_destination_reloads_as_failed_after_restart(settings):
    provider = Instagram()
    provider.create_response = httpx.Response(400, json={"error": {"message": "bad media"}})
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        assert create_instagram_post(client, account).status_code == 502
        token = client.cookies.get("relay_session")
    with TestClient(make_app(settings, provider)) as restarted:
        restarted.cookies.set("relay_session", token)
        rows = attempts(restarted)["attempts"]
        assert [row["status"] for row in rows] == ["failed"]
        assert rows[0]["next_action"] == "retry"
        assert rows[0]["error"]


def test_a_pending_receipt_that_never_started_is_not_left_in_flight(settings):
    """A receipt written but never claimed cannot have reached the provider."""
    store = Store(settings.database_path, settings.encryption_key)
    account = store.save_account(
        "youtube", "channel", "Channel", {"access_token": "t", "expires_in": 3600}, "scope"
    )
    store.create_attempt(
        account_id=account["id"],
        provider="youtube",
        status="pending",
        created_at=int(time.time()) - 3600,
    )
    store.reconcile_stale_attempts()
    with store.connection() as db:
        row = dict(db.execute("SELECT * FROM publishing_attempts").fetchone())
    assert row["status"] == "failed"
    assert "safe to try again" in row["error"]


def test_a_ready_container_is_never_aged_into_uncertainty(settings):
    """``ready`` is a known provider state, not a claim awaiting an answer."""
    store = Store(settings.database_path, settings.encryption_key)
    account = store.save_account(
        "instagram", "remote", "Channel", {"access_token": "t", "expires_in": 3600}, "scope"
    )
    post = store.create_post(account["id"], "container-1")
    store.update_post(post["id"], "ready")
    with store.connection() as db:
        db.execute(
            "UPDATE publishing_attempts SET claimed_at=? WHERE post_id=?",
            (int(time.time()) - 7200, post["id"]),
        )
    store.reconcile_stale_attempts()
    assert store.get_post(post["id"])["status"] == "ready"
    assert store.attempt_for_post(post["id"])["status"] == "ready"


def test_a_stale_claim_keeps_the_known_container_reference(settings):
    store = Store(settings.database_path, settings.encryption_key)
    account = store.save_account(
        "instagram", "remote", "Channel", {"access_token": "t", "expires_in": 3600}, "scope"
    )
    post = store.create_post(account["id"], "container-kept")
    store.update_post(post["id"], "ready")
    store.claim_post(post["id"])
    with store.connection() as db:
        db.execute(
            "UPDATE publishing_attempts SET claimed_at=? WHERE post_id=?",
            (int(time.time()) - 7200, post["id"]),
        )
    store.reconcile_stale_attempts()
    attempt = store.attempt_for_post(post["id"])
    assert attempt["status"] == "unknown"
    assert attempt["provider_ref"] == "container-kept"
    assert store.get_post(post["id"])["status"] == "unknown"


def test_a_duplicate_key_while_still_in_flight_returns_202_not_a_second_send(settings):
    """A double-click that arrives before the first container returns is not re-sent."""
    provider = Instagram()
    app = make_app(settings, provider)
    with TestClient(app) as client:
        signup(client)
        account = connect(client)
        store = app.state.store
        first = create_instagram_post(client, account, key="double-click-1")
        assert first.status_code == 201
        # Rewind the receipt to the in-flight shape the first request had mid-call.
        with store.connection() as db:
            db.execute("UPDATE publishing_attempts SET post_id=NULL, status='submitted'")
        again = create_instagram_post(client, account, key="double-click-1")
        assert again.status_code == 202
        assert again.json()["attempt"]["status"] == "submitted"
        assert provider.create_calls == 1
