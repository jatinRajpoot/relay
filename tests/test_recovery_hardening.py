"""Round 4 hardening: retry taxonomy, explicit recovery, storage fail-closed.

These tests target the seams the earlier rounds left open:

* a rate limit or revoked credential must not destroy a post;
* a retry must reach a real endpoint and keep one ledger row per submission;
* a cloud database that is missing must fail closed, never be re-created;
* an upload whose response was lost is resolved by a bounded reload.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from google.api_core.exceptions import GoogleAPIError, NotFound, PreconditionFailed
from test_publishing_recovery import (
    MEDIA,
    Instagram,
    YouTube,
    attempts,
    connect,
    create_instagram_post,
    make_app,
    publish_ready,
    signup,
    upload_youtube,
)
from test_service import HEADERS, youtube_provider

from relay.app import create_app
from relay.store import StorageError, Store

# ── Provider refusal taxonomy ────────────────────────────────────────────


def test_rate_limited_publish_stays_ready_and_retries_once(settings):
    provider = Instagram()
    provider.publish_response = httpx.Response(429, headers={"Retry-After": "120"})
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        first = publish_ready(client, post["id"], key="publish-key-429")

        assert first.status_code == 429
        assert first.headers["retry-after"] == "120"
        # The provider definitively did not publish, so the job must stay publishable.
        assert client.get(f"/instagram/posts/{post['id']}").json()["status"] == "ready"
        row = attempts(client)["attempts"][0]
        assert row["status"] == "failed"
        assert row["retry_kind"] == "delay"
        assert row["retry_at"] > int(time.time())
        assert row["next_action"] == "retry_later"
        assert provider.publish_calls == 1

        # Inside the cooldown the retry is refused rather than silently re-sent,
        # with or without the original key.
        blocked = publish_ready(client, post["id"], key="publish-key-429")
        assert blocked.status_code == 429
        assert publish_ready(client, post["id"]).status_code == 429
        assert provider.publish_calls == 1

        # Once the provider's cooldown has passed, the same key publishes once.
        with client.app.state.store.connection() as db:
            db.execute("UPDATE publishing_attempts SET retry_at=? WHERE post_id=?",
                       (int(time.time()) - 1, post["id"]))
        provider.publish_response = None
        retried = publish_ready(client, post["id"], key="publish-key-429")
        assert retried.status_code == 200, retried.text
        assert retried.json()["status"] == "published"
        assert provider.publish_calls == 2
        # One logical publish, one ledger row.
        assert len(attempts(client, status="all")["attempts"]) == 1


def test_revoked_credential_keeps_the_post_until_reconnect(settings):
    provider = Instagram()
    provider.publish_response = httpx.Response(401, json={"error": {"message": "expired"}})
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        first = publish_ready(client, post["id"], key="publish-key-auth")

        assert first.status_code == 409
        assert "Reconnect" in first.json()["detail"]
        assert client.get(f"/instagram/posts/{post['id']}").json()["status"] == "ready"
        row = attempts(client)["attempts"][0]
        assert row["status"] == "failed"
        assert row["retry_kind"] == "reconnect"
        assert row["next_action"] == "reconnect"

        # Still refused before the account is reconnected.
        assert publish_ready(client, post["id"], key="publish-key-auth").status_code == 409
        assert provider.publish_calls == 1

        # Reconnecting the channel clears the refusal in the same transaction, so
        # the same post and the same key become retryable without a new post.
        scope = client.app.state.store.workspace.set(
            client.get("/session").json()["workspace"]["id"]
        )
        try:
            client.app.state.store.save_account(
                "instagram", "professional-id", "My Studio IG",
                {"access_token": "ig-long", "expires_in": 5184000}, "scope",
            )
        finally:
            client.app.state.store.workspace.reset(scope)
        provider.publish_response = None
        retried = publish_ready(client, post["id"], key="publish-key-auth")
        assert retried.status_code == 200, retried.text
        assert provider.publish_calls == 2


def test_rate_limited_create_is_retryable_with_the_same_key(settings):
    provider = Instagram()
    provider.create_response = httpx.Response(429)
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        first = create_instagram_post(client, account, key="create-key-429")
        assert first.status_code == 429
        row = attempts(client)["attempts"][0]
        assert row["status"] == "failed"
        assert row["next_action"] == "retry_later"
        assert provider.create_calls == 1

        with client.app.state.store.connection() as db:
            db.execute("UPDATE publishing_attempts SET retry_at=?", (int(time.time()) - 1,))
        provider.create_response = None
        second = create_instagram_post(client, account, key="create-key-429")
        assert second.status_code == 201, second.text
        assert provider.create_calls == 2
        assert len(attempts(client, status="all")["attempts"]) == 1


def test_provider_timeout_is_uncertain_not_retryable(settings):
    """408 is not proof the provider did nothing, so it must stay unknown."""
    provider = Instagram()
    provider.publish_response = httpx.Response(408)
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        assert publish_ready(client, post["id"]).status_code == 502
        row = attempts(client)["attempts"][0]
        assert row["status"] == "unknown"
        assert row["next_action"] == "verify"
        assert publish_ready(client, post["id"]).status_code == 409
        assert provider.publish_calls == 1


@pytest.mark.parametrize("header", ["not-a-number", "-5", "999999", "Wed, 21 Oct 2026 07:28:00 GMT"])
def test_unusable_retry_after_falls_back_to_a_safe_default(settings, header):
    provider = Instagram()
    provider.publish_response = httpx.Response(429, headers={"Retry-After": header})
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        publish_ready(client, post["id"])
        row = attempts(client)["attempts"][0]
        # A bounded default, never a provider-supplied date or absurd wait.
        assert row["retry_at"] - row["updated_at"] == 60


def test_a_definite_rejection_is_still_retryable_and_retries_once(settings):
    provider = Instagram()
    provider.publish_response = httpx.Response(400, json={"error": {"message": "bad"}})
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        assert publish_ready(client, post["id"], key="publish-key-400").status_code == 502
        row = attempts(client)["attempts"][0]
        assert row["status"] == "failed"
        assert row["next_action"] == "retry"
        provider.publish_response = None
        retried = publish_ready(client, post["id"], key="publish-key-400")
        assert retried.status_code == 200, retried.text
        assert provider.publish_calls == 2
        assert len(attempts(client, status="all")["attempts"]) == 1


def test_a_failed_upload_retries_with_the_same_key(settings):
    provider = YouTube()
    provider.upload_response = httpx.Response(403, json={
        "error": {"errors": [{"reason": "quotaExceeded"}]}
    })
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client, "youtube")
        first = upload_youtube(client, account, key="upload-key-quota")
        assert first.status_code == 429
        row = attempts(client)["attempts"][0]
        assert row["status"] == "failed"
        assert row["retry_kind"] == "delay"
        assert provider.uploads == 1

        with client.app.state.store.connection() as db:
            db.execute("UPDATE publishing_attempts SET retry_at=?", (int(time.time()) - 1,))
        provider.upload_response = None
        second = upload_youtube(client, account, key="upload-key-quota")
        assert second.status_code == 201, second.text
        assert second.json()["id"] == "video-1"
        assert provider.uploads == 2
        assert len(attempts(client, status="all")["attempts"]) == 1


# ── Failure after a known provider success ───────────────────────────────


def test_history_write_failure_does_not_turn_a_success_into_a_retry(settings):
    provider = Instagram()
    app = make_app(settings, provider)
    with TestClient(app) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        store = app.state.store
        original = store._save_instagram_history

        def refuse(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        store._save_instagram_history = staticmethod(refuse)
        try:
            response = publish_ready(client, post["id"])
        finally:
            store._save_instagram_history = staticmethod(original)

        # The provider published. A local history failure must not report a failure
        # the user would "fix" by publishing a second time.
        assert response.status_code == 200
        assert response.json()["status"] == "published"
        assert provider.publish_calls == 1
        row = attempts(client, status="all")["attempts"][0]
        assert row["status"] == "published"
        assert "history" in row["error"]


# ── Concurrent duplicate submission ──────────────────────────────────────


def test_simultaneous_same_key_creates_dispatch_once(settings):
    provider = Instagram()
    app = make_app(settings, provider)

    async def scenario():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                store = app.state.store
                account = store.save_account(
                    "instagram", "prof", "IG",
                    {"access_token": "ig-token", "expires_in": 5184000}, "scope",
                )
                entered, release = asyncio.Event(), asyncio.Event()

                async def slow_create(*args, **kwargs):
                    entered.set()
                    await release.wait()
                    return "container-1"

                app.state.providers.create_instagram_post = slow_create
                payload = {
                    "account_id": account["id"],
                    "media_type": "IMAGE",
                    "media_url": MEDIA,
                    "caption": "Hello",
                }
                headers = HEADERS | {"Idempotency-Key": "race-key-0001"}
                first = asyncio.create_task(
                    client.post("/instagram/posts", json=payload, headers=headers)
                )
                await asyncio.wait_for(entered.wait(), 5)
                second = asyncio.create_task(
                    client.post("/instagram/posts", json=payload, headers=headers)
                )
                await asyncio.sleep(0.2)
                release.set()
                return (
                    await asyncio.wait_for(first, 5),
                    await asyncio.wait_for(second, 5),
                )

    first, second = asyncio.run(scenario())
    assert {first.status_code, second.status_code} <= {201, 202}
    # Exactly one container was created for one logical submission.
    with sqlite3.connect(settings.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM publishing_attempts").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM instagram_posts").fetchone()[0] == 1


def test_a_pending_receipt_racing_an_active_claim_is_not_double_dispatched(settings):
    """A stale sweep must not release a claim another request is actively using."""
    provider = Instagram()
    app = make_app(settings, provider)
    with TestClient(app) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        store = app.state.store
        # A publish claim taken one second ago is still active.
        with store.connection() as db:
            db.execute("UPDATE instagram_posts SET status='publishing' WHERE id=?", (post["id"],))
            db.execute(
                "UPDATE publishing_attempts SET status='submitted', claimed_at=? WHERE post_id=?",
                (int(time.time()) - 1, post["id"]),
            )
        store.reconcile_stale_attempts()
        row = attempts(client)["attempts"][0]
        assert row["status"] == "submitted"
        assert row["next_action"] == "check"
        assert provider.publish_calls == 0


# ── Refresh / disconnect race ────────────────────────────────────────────


def test_disconnect_during_refresh_returns_404_not_500(settings):
    app = create_app(settings, httpx.MockTransport(youtube_provider))

    async def scenario():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                store = app.state.store
                account = store.save_account(
                    "youtube", "chan-1", "Channel",
                    {"access_token": "a", "refresh_token": "r", "expires_in": 3600}, "scope",
                )
                entered, release = asyncio.Event(), asyncio.Event()

                async def slow_refresh(*args, **kwargs):
                    entered.set()
                    await release.wait()
                    return {"access_token": "renewed", "expires_in": 3600}

                app.state.providers.refresh = slow_refresh
                refresh = asyncio.create_task(
                    client.post(f"/accounts/{account['id']}/refresh", headers=HEADERS)
                )
                await asyncio.wait_for(entered.wait(), 5)
                deleted = await client.delete(f"/accounts/{account['id']}", headers=HEADERS)
                release.set()
                holder = await asyncio.wait_for(refresh, 5)
                return deleted, holder, store.get_account(account["id"])

    deleted, holder, remaining = asyncio.run(scenario())
    assert deleted.status_code == 204
    # The holder must not resurrect the account, and must not answer with a 500.
    assert holder.status_code == 404
    assert remaining is None


# ── Cloud storage: fail closed ───────────────────────────────────────────


class Blob:
    """A fake blob with generation CAS and controllable failures."""

    def __init__(self, data=b"", generation=0):
        self.data = data
        self.generation = generation
        self.missing = generation == 0
        self.upload_error = None
        self.upload_applies = True
        self.reload_error = None
        self.fail_reloads_after_upload = False
        self.upload_attempted = False

    def reload(self):
        if self.reload_error:
            raise self.reload_error
        if self.fail_reloads_after_upload and self.upload_attempted:
            raise GoogleAPIError("still failing")
        if self.missing:
            raise NotFound("missing")

    def download_as_bytes(self, if_generation_match=None):
        if self.missing:
            raise NotFound("missing")
        if if_generation_match is not None and if_generation_match != self.generation:
            raise PreconditionFailed("stale")
        return self.data

    def upload_from_string(self, data, content_type=None, if_generation_match=None):
        if if_generation_match is not None and if_generation_match != self.generation:
            raise PreconditionFailed("stale")
        self.upload_attempted = True
        if self.upload_error:
            if self.upload_applies:
                self.data, self.generation, self.missing = data, self.generation + 1, False
            raise self.upload_error
        self.data, self.generation, self.missing = data, self.generation + 1, False


@pytest.fixture
def blob(monkeypatch, settings):
    from google.cloud import storage

    fake = Blob()
    monkeypatch.setenv("DATABASE_BUCKET", "test-private-bucket")
    monkeypatch.setattr(storage, "Client", lambda: type("Client", (), {
        "bucket": lambda self, name: type("Bucket", (), {
            "blob": lambda self, name: fake,
        })(),
    })())
    return fake


def _initialized(settings, blob):
    assert Store.initialize_cloud_snapshot(settings.database_path, settings.encryption_key)
    store = Store(settings.database_path, settings.encryption_key)
    store.save_account(
        "youtube", "chan", "Channel", {"access_token": "t", "expires_in": 3600}, "scope"
    )
    return store


def test_missing_snapshot_fails_closed_at_startup(settings, blob):
    with pytest.raises(StorageError) as raised:
        Store(settings.database_path, settings.encryption_key)
    assert raised.value.outcome == "not_applied"
    assert blob.generation == 0  # nothing was uploaded
    assert not settings.database_path.exists()


def test_missing_snapshot_after_use_never_recreates_it(settings, blob):
    _initialized(settings, blob)
    cached = settings.database_path.read_bytes()
    blob.missing = True
    blob.generation = 0

    with pytest.raises(StorageError):
        Store(settings.database_path, settings.encryption_key)

    assert blob.generation == 0
    # The local diagnostic copy is preserved, not truncated by an empty download.
    assert settings.database_path.read_bytes() == cached


def test_fresh_process_without_local_cache_fails_closed(settings, blob):
    _initialized(settings, blob)
    settings.database_path.unlink()
    blob.missing = True
    with pytest.raises(StorageError):
        Store(settings.database_path, settings.encryption_key)
    assert not settings.database_path.exists()


@pytest.mark.parametrize("payload", [b"", b"not a database", b"SQLite format 3\x00truncated"])
def test_empty_or_corrupt_download_is_rejected_and_cache_kept(settings, blob, payload):
    _initialized(settings, blob)
    cached = settings.database_path.read_bytes()
    blob.data = payload
    blob.generation += 1

    with pytest.raises(StorageError):
        Store(settings.database_path, settings.encryption_key)

    assert settings.database_path.read_bytes() == cached


def test_explicit_init_is_a_noop_when_a_snapshot_exists(settings, blob):
    store = _initialized(settings, blob)
    before = blob.data
    generation = blob.generation

    assert Store.initialize_cloud_snapshot(settings.database_path, settings.encryption_key) is False

    assert blob.data == before
    assert blob.generation == generation
    again = Store(settings.database_path, settings.encryption_key)
    assert again.list_accounts()[0]["external_id"] == "chan"
    assert store.list_accounts()[0]["external_id"] == "chan"


def test_runtime_construction_never_creates_a_snapshot(settings, blob):
    """The old ``DATABASE_INIT=create`` runtime bypass must not exist any more.

    A leftover flag in the service environment must not turn a missing snapshot
    into a fresh empty database: only the explicit class method may create one.
    """
    with pytest.raises(TypeError):
        Store(settings.database_path, settings.encryption_key, database_init="create")
    with pytest.raises(StorageError):
        Store(settings.database_path, settings.encryption_key)
    assert blob.generation == 0
    assert not settings.database_path.exists()


def test_initialization_cannot_replace_a_snapshot_created_concurrently(settings, blob):
    """Two operators racing must produce exactly one snapshot, never a replace."""
    assert Store.initialize_cloud_snapshot(settings.database_path, settings.encryption_key)
    blob.data = b"SQLite format 3\x00someone-else"
    blob.generation = 7
    before = blob.data

    assert Store.initialize_cloud_snapshot(settings.database_path, settings.encryption_key) is False
    assert blob.data == before
    assert blob.generation == 7


def test_lost_upload_response_is_resolved_to_success(settings, blob):
    store = _initialized(settings, blob)
    blob.upload_error = GoogleAPIError("response lost")
    blob.upload_applies = True

    # The write is durable even though the caller saw an error; the bounded reload
    # proves the exact intended bytes are stored.
    store.save_account(
        "youtube", "chan-2", "Second", {"access_token": "t2", "expires_in": 3600}, "scope"
    )
    reopened = Store(settings.database_path, settings.encryption_key)
    assert {row["external_id"] for row in reopened.list_accounts()} == {"chan", "chan-2"}


def test_rejected_upload_reports_not_applied(settings, blob):
    store = _initialized(settings, blob)
    blob.upload_error = GoogleAPIError("denied")
    blob.upload_applies = False
    with pytest.raises(StorageError) as raised:
        store.save_account(
            "youtube", "chan-2", "Second", {"access_token": "t2", "expires_in": 3600}, "scope"
        )
    assert raised.value.outcome == "not_applied"


def test_unverifiable_upload_reports_unknown(settings, blob):
    store = _initialized(settings, blob)
    # The upload fails and every confirming read fails too, so the outcome is
    # genuinely indeterminate. It must not be reported as "not applied".
    blob.upload_error = GoogleAPIError("denied")
    blob.upload_applies = False
    blob.upload_attempted = False
    blob.fail_reloads_after_upload = True
    with pytest.raises(StorageError) as raised:
        store.save_account(
            "youtube", "chan-2", "Second", {"access_token": "t2", "expires_in": 3600}, "scope"
        )
    assert raised.value.outcome == "unknown"


def test_stale_writer_never_overwrites_a_newer_generation(settings, blob):
    store = _initialized(settings, blob)
    before = blob.data
    with pytest.raises(StorageError) as raised, store.connection() as db:
        db.execute("CREATE TABLE race_test (id INTEGER)")
        blob.generation += 1  # another instance wrote while we were working
    assert raised.value.outcome == "unknown"
    assert blob.data == before


def test_storage_failure_is_a_sanitised_503(settings):
    app = create_app(settings, httpx.MockTransport(youtube_provider))
    with TestClient(app) as client:
        def refuse():
            raise StorageError("The cloud database is temporarily unavailable.",
                               outcome="not_applied")

        app.state.store.list_accounts = refuse
        response = client.get("/accounts", headers=HEADERS)
        assert response.status_code == 503
        assert response.json() == {
            "detail": "The cloud database is temporarily unavailable.",
            "outcome": "not_applied",
        }
        assert "google" not in response.text.lower()
        assert "Traceback" not in response.text


# ── Cursor and workspace scoping ─────────────────────────────────────────


def test_cursor_spans_multiple_providers_at_one_timestamp(settings):
    store = Store(settings.database_path, settings.encryption_key)
    account = store.save_account(
        "youtube", "chan", "Channel", {"access_token": "t", "expires_in": 3600}, "scope"
    )
    stamp = 1_700_000_000
    ids = []
    for index, provider in enumerate(["youtube", "instagram", "youtube", "instagram"]):
        ids.append(store.create_attempt(
            account_id=account["id"], provider=provider, status="failed",
            created_at=stamp, updated_at=stamp, attempt_id=f"mixed-{index}",
        )["id"])
    with TestClient(make_app(settings, youtube_provider)) as client:
        seen, cursor = [], None
        while True:
            params = {"status": "all", "limit": 1, "headers": HEADERS}
            if cursor:
                params["cursor"] = cursor
            page = attempts(client, **params)
            seen.extend(row["id"] for row in page["attempts"])
            cursor = page["next_cursor"]
            if not cursor:
                break
        assert seen == sorted(ids, reverse=True)
        assert len(seen) == len(set(seen)) == 4


def test_the_same_key_on_two_accounts_does_not_collide(settings):
    provider = Instagram()
    app = make_app(settings, provider)
    with TestClient(app) as client:
        store = app.state.store
        signup(client)
        first = connect(client)
        scope = store.workspace.set(client.get("/session").json()["workspace"]["id"])
        try:
            second = store.save_account(
                "instagram", "second-professional", "Second",
                {"access_token": "ig-2", "expires_in": 5184000}, "scope",
            )
        finally:
            store.workspace.reset(scope)
        assert second["id"] != first["id"]

        def handler(request):
            if request.url.path == "/v25.0/second-professional/media":
                return httpx.Response(200, json={"id": "container-second"})
            return provider(request)

        app.state.providers.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        assert create_instagram_post(client, first, key="shared-key-01").status_code == 201
        assert create_instagram_post(client, second, key="shared-key-01").status_code == 201
        assert provider.create_calls == 1  # the second account used its own handler
