"""Round 5 contract: exact receipt correlation, byte-bound uploads, durable success.

These tests pin the seams a lost response exposes:

* a receipt can be correlated to the client's intent by ``request_key`` alone —
  never by timestamps, which can attach an unrelated newer/simultaneous post;
* two different videos of identical size/name/type are two different requests;
* a provider success that cannot be recorded is never reported as a plain
  success, and never becomes a second publish;
* a runtime ``DATABASE_INIT=create`` bypass does not exist.
"""

from __future__ import annotations

import sqlite3
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from test_publishing_recovery import (
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
from test_service import HEADERS

from relay.store import StorageError, Store
from relay.store.attempts import fingerprint

# ── request_key: exact correlation, workspace-scoped ─────────────────────


def test_receipt_exposes_the_exact_create_key_for_correlation(settings):
    provider = Instagram()
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        created = create_instagram_post(client, account, key="intent-key-0001")
        assert created.status_code == 201

        rows = attempts(client)["attempts"]

        assert len(rows) == 1
        assert rows[0]["request_key"] == "intent-key-0001"
        assert rows[0]["post_id"] == created.json()["id"]
        assert rows[0]["account_id"] == account["id"]
        assert rows[0]["provider"] == "instagram"


def test_receipt_exposes_the_exact_upload_key_for_correlation(settings):
    provider = YouTube()
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client, "youtube")
        uploaded = upload_youtube(client, account, key="upload-intent-01")
        assert uploaded.status_code == 201

        rows = attempts(client, status="all")["attempts"]

        assert rows[0]["request_key"] == "upload-intent-01"
        assert rows[0]["status"] == "published"


def test_a_receipt_can_be_found_by_key_after_the_response_is_lost(settings):
    """The exact scenario timestamp matching gets wrong is now exact."""
    provider = Instagram()
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        # A prior, unrelated post in the same account and the same second.
        assert create_instagram_post(client, account, key="older-intent-01").status_code == 201
        lost = create_instagram_post(client, account, key="lost-intent-001")
        assert lost.status_code == 201
        # A simultaneous post that is *not* the one whose response was lost.
        assert create_instagram_post(client, account, key="other-intent-01").status_code == 201

        rows = attempts(client, status="all")["attempts"]
        matched = [row for row in rows if row["request_key"] == "lost-intent-001"]

        assert len(matched) == 1
        assert matched[0]["post_id"] == lost.json()["id"]
        # The other two are distinguishable, even at the same created_at second.
        assert {row["request_key"] for row in rows} == {
            "older-intent-01", "lost-intent-001", "other-intent-01",
        }


def test_keyless_and_legacy_receipts_have_a_null_request_key(settings):
    store = Store(settings.database_path, settings.encryption_key)
    account = store.save_account(
        "youtube", "chan", "Channel", {"access_token": "t", "expires_in": 3600}, "scope"
    )
    store.create_attempt(account_id=account["id"], provider="youtube", status="failed")
    with TestClient(make_app(settings, YouTube())) as client:
        rows = attempts(client, status="all", headers=HEADERS)["attempts"]
        assert rows[0]["request_key"] is None


def test_request_key_never_crosses_a_workspace_boundary(settings):
    provider = Instagram()
    app = make_app(settings, provider)
    with TestClient(app) as alice, TestClient(app) as bob:
        signup(alice)
        alice_account = connect(alice)
        alice_post = create_instagram_post(alice, alice_account, key="alice-intent-01").json()
        alice_attempt = attempts(alice)["attempts"][0]

        signup(bob, "bob@example.com")

        # Bob cannot list, read, or address Alice's receipt, with or without her key.
        assert attempts(bob)["attempts"] == []
        assert bob.get(f"/publishing/attempts/{alice_attempt['id']}").status_code == 404
        assert bob.get(
            "/publishing/attempts", params={"cursor": alice_attempt["id"]}
        ).status_code == 422
        # The key is not a capability: using it against Alice's account is a 404,
        # because the account itself is not in Bob's workspace.
        assert create_instagram_post(bob, alice_account, key="alice-intent-01").status_code == 404
        # Alice's receipt is untouched and still exactly correlatable.
        assert [row["request_key"] for row in attempts(alice)["attempts"]] == ["alice-intent-01"]
        assert provider.create_calls == 1
        assert alice_post["id"] == attempts(alice)["attempts"][0]["post_id"]


def test_both_client_keys_are_exposed_but_no_fingerprint_is(settings):
    provider = Instagram()
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account, key="create-intent-01").json()
        assert publish_ready(client, post["id"], key="publish-intent-1").status_code == 200

        row = attempts(client, status="all")["attempts"][0]
        text = client.get("/publishing/attempts", params={"status": "all"}).text

        # Both are the client's own correlation values, so both are returned.
        assert row["request_key"] == "create-intent-01"
        assert row["publish_key"] == "publish-intent-1"
        # The server-side payload hashes are never exposed.
        assert "fingerprint" not in text
        assert "request_fingerprint" not in text
        assert "publish_fingerprint" not in text


# ── Explicit manual recovery: the action matches the endpoint ────────────


def test_failed_container_create_offers_the_create_endpoint(settings):
    provider = Instagram()
    provider.create_response = httpx.Response(400, json={"error": {"message": "bad media"}})
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        assert create_instagram_post(client, account, key="create-fail-01").status_code == 502

        row = attempts(client)["attempts"][0]

        assert row["status"] == "failed"
        assert row["next_action"] == "retry"
        # No job exists, so the publish endpoint would 404/409. The create is the
        # only request that can implement this retry.
        assert row["retry_action"] == {
            "kind": "resend",
            "endpoint": "/instagram/posts",
            "key": "request",
            "retry_at": None,
        }


def test_failed_publish_offers_the_publish_endpoint_not_the_create(settings):
    provider = Instagram()
    provider.publish_response = httpx.Response(400, json={"error": {"message": "bad"}})
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account, key="create-ok-0001").json()
        assert publish_ready(client, post["id"], key="publish-fail-1").status_code == 502

        row = attempts(client)["attempts"][0]

        assert row["status"] == "failed"
        assert row["next_action"] == "retry"
        # The container still exists. Repeating the create would make a second one.
        assert row["retry_action"] == {
            "kind": "resend",
            "endpoint": f"/instagram/posts/{post['id']}/publish",
            "key": "publish",
            "retry_at": None,
        }


def test_failed_upload_offers_the_upload_endpoint(settings):
    provider = YouTube()
    provider.upload_response = httpx.Response(400, json={"error": {"message": "invalid"}})
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client, "youtube")
        assert upload_youtube(client, account, key="upload-fail-01").status_code == 502

        row = attempts(client)["attempts"][0]

        assert row["retry_action"] == {
            "kind": "resend",
            "endpoint": "/youtube/videos",
            "key": "request",
            "retry_at": None,
        }


def test_a_rate_limited_failure_offers_a_wait_not_a_resend(settings):
    provider = Instagram()
    provider.publish_response = httpx.Response(429, headers={"Retry-After": "90"})
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        assert publish_ready(client, post["id"]).status_code == 429

        row = attempts(client)["attempts"][0]

        assert row["status"] == "failed"
        assert row["next_action"] == "retry_later"
        # One usable action, and it is the cooldown: no endpoint, the exact
        # server-side retry_at, and no resend recipe that contradicts it.
        assert row["retry_action"] == {
            "kind": "wait",
            "endpoint": None,
            "key": "none",
            "retry_at": row["retry_at"],
        }
        assert row["retry_action"]["retry_at"] > row["updated_at"]


def test_a_cooldown_becomes_a_resend_only_when_it_expires(settings):
    """The wait recipe must flip to a working resend at exactly retry_at."""
    provider = Instagram()
    provider.publish_response = httpx.Response(429, headers={"Retry-After": "90"})
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        assert publish_ready(client, post["id"], key="cooldown-key-1").status_code == 429

        waiting = attempts(client)["attempts"][0]
        assert waiting["retry_action"]["kind"] == "wait"

        # Before the cooldown expires the endpoint refuses; the recipe says so.
        assert publish_ready(client, post["id"], key="cooldown-key-1").status_code == 429
        assert provider.publish_calls == 1

        with client.app.state.store.connection() as db:
            db.execute(
                "UPDATE publishing_attempts SET retry_at=? WHERE post_id=?",
                (int(time.time()) - 1, post["id"]),
            )
        provider.publish_response = None
        ready = attempts(client)["attempts"][0]
        assert ready["retry_action"] == {
            "kind": "resend",
            "endpoint": f"/instagram/posts/{post['id']}/publish",
            "key": "publish",
            "retry_at": None,
        }
        assert ready["next_action"] == "retry"

        retried = publish_ready(client, post["id"], key=ready["publish_key"])
        assert retried.status_code == 200, retried.text
        assert provider.publish_calls == 2
        assert len(attempts(client, status="all")["attempts"]) == 1


def test_a_keyless_failed_publish_is_retryable_without_a_key(settings):
    provider = Instagram()
    provider.publish_response = httpx.Response(400, json={"error": {"message": "bad"}})
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        assert publish_ready(client, post["id"]).status_code == 502

        row = attempts(client)["attempts"][0]
        assert row["publish_key"] is None
        assert row["retry_action"] == {
            "kind": "resend",
            "endpoint": f"/instagram/posts/{post['id']}/publish",
            "key": "none",
            "retry_at": None,
        }
        provider.publish_response = None
        assert publish_ready(client, post["id"]).status_code == 200
        assert provider.publish_calls == 2


def test_a_revoked_credential_offers_a_reconnect_not_a_resend(settings):
    provider = Instagram()
    provider.publish_response = httpx.Response(401, json={"error": {"message": "expired"}})
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        assert publish_ready(client, post["id"]).status_code == 409

        row = attempts(client)["attempts"][0]

        assert row["next_action"] == "reconnect"
        assert row["retry_action"] == {
            "kind": "reconnect", "endpoint": None, "key": "none", "retry_at": None,
        }


def test_an_unknown_outcome_never_offers_a_resend(settings):
    provider = Instagram()
    provider.publish_response = httpx.Response(503, json={"error": {"message": "upstream"}})
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        assert publish_ready(client, post["id"]).status_code == 502

        row = attempts(client)["attempts"][0]

        assert row["status"] == "unknown"
        assert row["next_action"] == "verify"
        # Not a failed receipt, so there is no retry recipe at all.
        assert row["retry_action"] is None
        assert row["verification_url"] == "https://www.instagram.com"


def test_a_failed_publish_whose_job_is_unknown_offers_a_new_attempt(settings):
    """The receipt can say "failed" while the job is no longer safe to re-publish."""
    provider = Instagram()
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        store = client.app.state.store
        with store.connection() as db:
            db.execute("UPDATE instagram_posts SET status='unknown' WHERE id=?", (post["id"],))
            db.execute(
                "UPDATE publishing_attempts SET status='failed', retry_kind='none' "
                "WHERE post_id=?", (post["id"],),
            )

        row = attempts(client)["attempts"][0]

        # The publish endpoint would refuse this job, so the UI must not offer a
        # resend. Edit-a-copy is the only honest option.
        assert row["next_action"] == "edit_copy"
        assert row["retry_action"]["kind"] == "new_attempt"
        assert row["retry_action"]["endpoint"] is None


def test_the_advertised_retry_endpoint_actually_works(settings):
    """Every ``resend`` must be executable: following it succeeds, not 409."""
    provider = Instagram()
    provider.create_response = httpx.Response(400, json={"error": {"message": "bad"}})
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        assert create_instagram_post(client, account, key="follow-me-001").status_code == 502
        row = attempts(client)["attempts"][0]
        assert row["retry_action"]["kind"] == "resend"

        provider.create_response = None
        retried = create_instagram_post(client, account, key=row["request_key"])

        assert retried.status_code == 201, retried.text
        assert provider.create_calls == 2
        assert len(attempts(client, status="all")["attempts"]) == 1


def test_a_publish_retry_follows_the_advertised_endpoint(settings):
    provider = Instagram()
    provider.publish_response = httpx.Response(400, json={"error": {"message": "bad"}})
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        assert publish_ready(client, post["id"], key="publish-follow-1").status_code == 502
        row = attempts(client)["attempts"][0]
        assert row["retry_action"]["endpoint"] == f"/instagram/posts/{post['id']}/publish"

        provider.publish_response = None
        retried = publish_ready(client, post["id"], key="publish-follow-1")

        assert retried.status_code == 200, retried.text
        assert retried.json()["status"] == "published"
        assert provider.publish_calls == 2
        assert len(attempts(client, status="all")["attempts"]) == 1


# ── Malformed keys are a 422, never a 500 ────────────────────────────────


@pytest.mark.parametrize("key", ["short", "bad key!!", "x" * 129, "tab\tkey"])
def test_a_malformed_create_header_is_rejected_before_dispatch(settings, key):
    provider = Instagram()
    app = make_app(settings, provider)
    with TestClient(app) as client:
        account = app.state.store.save_account(
            "instagram", "prof", "IG", {"access_token": "t", "expires_in": 5184000}, "scope"
        )
        response = client.post(
            "/instagram/posts",
            headers=HEADERS | {"Idempotency-Key": key},
            json={
                "account_id": account["id"],
                "media_type": "IMAGE",
                "media_url": "https://cdn.example/media",
            },
        )
        assert response.status_code == 422, response.text
        assert provider.create_calls == 0
        assert attempts(client, headers=HEADERS)["attempts"] == []


@pytest.mark.parametrize("key", ["short", "bad key!!", "x" * 129])
def test_a_malformed_upload_header_is_rejected_before_dispatch(settings, key):
    provider = YouTube()
    app = make_app(settings, provider)
    with TestClient(app) as client:
        account = app.state.store.save_account(
            "youtube", "chan", "Channel", {"access_token": "t", "expires_in": 3600}, "scope"
        )
        response = client.post(
            "/youtube/videos",
            headers=HEADERS | {"Idempotency-Key": key},
            data={"account_id": account["id"], "title": "Test"},
            files={"file": ("video.mp4", b"data", "video/mp4")},
        )
        assert response.status_code == 422, response.text
        assert provider.uploads == 0
        assert attempts(client, headers=HEADERS)["attempts"] == []


def test_a_malformed_publish_header_is_rejected_before_dispatch(settings):
    provider = Instagram()
    app = make_app(settings, provider)
    with TestClient(app) as client:
        account = app.state.store.save_account(
            "instagram", "prof", "IG", {"access_token": "t", "expires_in": 5184000}, "scope"
        )
        post = app.state.store.create_post(account["id"], "container-1")
        response = client.post(
            f"/instagram/posts/{post['id']}/publish",
            headers=HEADERS | {"Idempotency-Key": "bad key!!"},
        )
        assert response.status_code == 422, response.text
        assert provider.publish_calls == 0


# ── One usable action: projection and endpoint always agree ──────────────

# The single source of truth the interface may render. ``next_action`` and
# ``retry_action`` are both derived from the same decision, so a state can never
# advertise "retry" while the recipe says "wait", or offer a resend the endpoint
# would refuse.
_CONSISTENT = {
    "resend": "retry",
    "wait": "retry_later",
    "reconnect": "reconnect",
    "new_attempt": "edit_copy",
}


def _row(**overrides):
    base = {
        "id": "attempt-1", "provider": "youtube", "account_id": "acct",
        "account_name": "Channel", "status": "failed", "post_id": None,
        "provider_ref": None, "provider_url": None, "media_name": None,
        "media_type": None, "media_url": None, "title": None, "caption": "",
        "privacy_status": None, "error": None, "retry_kind": "none",
        "retry_at": None, "created_at": 1, "updated_at": 1, "claimed_at": None,
        "idempotency_key": None, "publish_idempotency_key": None,
        "request_fingerprint": None, "publish_fingerprint": None,
        "post_status": None, "account_token_updated_at": 1,
    }
    return base | overrides


@pytest.mark.parametrize(
    "row",
    [
        # A keyed YouTube refusal that may be retried now.
        _row(idempotency_key="upload-key-01"),
        # The same refusal, keyless: still resendable, but with no key to reuse.
        _row(),
        # A keyed Instagram create refusal (no job).
        _row(provider="instagram", idempotency_key="create-key-01"),
        # A keyed Instagram publish refusal (job still usable).
        _row(provider="instagram", post_id="post-1", post_status="ready",
             idempotency_key="create-key-01", publish_idempotency_key="publish-key-1"),
        # A publish refusal whose job is no longer safe to re-publish.
        _row(provider="instagram", post_id="post-1", post_status="unknown",
             idempotency_key="create-key-01", publish_idempotency_key="publish-key-1"),
        # A cooldown.
        _row(retry_kind="delay", retry_at=2_000_000_000),
        # A cooldown on a keyed upload.
        _row(retry_kind="delay", retry_at=2_000_000_000, idempotency_key="upload-key-01"),
        # A revoked credential.
        _row(retry_kind="reconnect", idempotency_key="upload-key-01"),
        # Not failed: no retry recipe at all.
        _row(status="unknown", provider="instagram", post_id="post-1"),
        _row(status="published", provider="youtube"),
        _row(status="ready", provider="instagram", post_id="post-1"),
        _row(status="pending"),
    ],
    ids=[
        "yt-keyed", "yt-keyless", "ig-create", "ig-publish", "ig-publish-unsafe",
        "cooldown", "cooldown-keyed", "reconnect", "unknown", "published", "ready", "pending",
    ],
)
def test_the_projection_never_contradicts_itself(row):
    from relay.store.attempts import AttemptStore

    projected = AttemptStore.public_attempt(row)
    action = projected["retry_action"]
    expected = _CONSISTENT.get(action["kind"]) if action else None

    if projected["status"] != "failed":
        assert action is None
        assert projected["next_action"] not in {"retry", "retry_later", "reconnect"}
        return

    assert action is not None
    assert projected["next_action"] == expected, "next_action and retry_action disagree"
    if action["kind"] == "resend":
        # A resend must name a real endpoint and a key the client can supply.
        assert action["endpoint"] in {
            "/instagram/posts", "/youtube/videos",
            f"/instagram/posts/{row['post_id']}/publish",
        }
        assert action["key"] in {"request", "publish", "none"}
        if action["key"] == "request":
            assert projected["request_key"], "recipe names a request key that is not exposed"
        if action["key"] == "publish":
            assert projected["publish_key"], "recipe names a publish key that is not exposed"
        assert action["retry_at"] is None
    else:
        assert action["endpoint"] is None
        assert action["key"] == "none"
    if action["kind"] == "wait":
        assert action["retry_at"] == projected["retry_at"]
        assert action["retry_at"] > row["updated_at"]
    if action["kind"] == "reconnect":
        assert projected["retry_kind"] == "reconnect"


def test_a_keyless_failed_create_is_retryable_but_not_deduplicated(settings):
    """No key is not a reason to refuse a retry; it only means "don't send one".

    A keyless retry is an honest new submission: the endpoint accepts it, and
    because there is no key there is no ledger row to merge into. The recipe says
    exactly that (``key: "none"``) so the interface can explain it rather than
    imply duplicate protection it cannot provide.
    """
    provider = Instagram()
    provider.create_response = httpx.Response(400, json={"error": {"message": "bad"}})
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        assert create_instagram_post(client, account).status_code == 502

        row = attempts(client)["attempts"][0]

        assert row["request_key"] is None
        assert row["next_action"] == "retry"
        assert row["retry_action"] == {
            "kind": "resend", "endpoint": "/instagram/posts", "key": "none", "retry_at": None,
        }
        provider.create_response = None
        retried = create_instagram_post(client, account)
        assert retried.status_code == 201, retried.text
        assert provider.create_calls == 2
        # Two unkeyed submissions are two ledger rows, not a silent merge.
        assert len(attempts(client, status="all")["attempts"]) == 2


def test_a_keyless_failed_upload_is_still_exactly_retryable(settings):
    provider = YouTube()
    provider.upload_response = httpx.Response(400, json={"error": {"message": "invalid"}})
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client, "youtube")
        assert upload_youtube(client, account).status_code == 502

        row = attempts(client)["attempts"][0]

        assert row["request_key"] is None
        assert row["retry_action"]["key"] == "none"
        provider.upload_response = None
        assert upload_youtube(client, account).status_code == 201
        assert provider.uploads == 2


# ── publish_key: recover a keyed confirmation after draft loss ───────────


def test_a_failed_publish_exposes_the_exact_key_to_reuse(settings):
    provider = Instagram()
    provider.publish_response = httpx.Response(400, json={"error": {"message": "bad"}})
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        assert publish_ready(client, post["id"], key="publish-lost-01").status_code == 502

        row = attempts(client)["attempts"][0]

        # The client lost its draft. It can still retry with the exact original
        # key, so the retry stays one ledger row rather than a fresh submission.
        assert row["publish_key"] == "publish-lost-01"
        assert row["retry_action"]["key"] == "publish"


def test_a_publish_key_recovers_after_a_full_reload(settings):
    provider = Instagram()
    provider.publish_response = httpx.Response(400, json={"error": {"message": "bad"}})
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        assert publish_ready(client, post["id"], key="publish-reload-1").status_code == 502
        token = client.cookies.get("relay_session")

    provider.publish_response = None
    with TestClient(make_app(settings, provider)) as restarted:
        restarted.cookies.set("relay_session", token)
        row = attempts(restarted)["attempts"][0]
        assert row["publish_key"] == "publish-reload-1"

        retried = publish_ready(restarted, post["id"], key=row["publish_key"])

        assert retried.status_code == 200, retried.text
        assert provider.publish_calls == 2
        assert len(attempts(restarted, status="all")["attempts"]) == 1


def test_publish_key_is_null_before_a_publish_is_claimed(settings):
    provider = Instagram()
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        create_instagram_post(client, account, key="create-only-01")
        row = attempts(client)["attempts"][0]
        assert row["request_key"] == "create-only-01"
        assert row["publish_key"] is None


def test_publish_key_never_crosses_a_workspace_boundary(settings):
    provider = Instagram()
    provider.publish_response = httpx.Response(400, json={"error": {"message": "bad"}})
    app = make_app(settings, provider)
    with TestClient(app) as alice, TestClient(app) as bob:
        signup(alice)
        alice_account = connect(alice)
        post = create_instagram_post(alice, alice_account).json()
        assert publish_ready(alice, post["id"], key="alice-publish-1").status_code == 502
        alice_attempt = attempts(alice)["attempts"][0]
        assert alice_attempt["publish_key"] == "alice-publish-1"

        signup(bob, "bob@example.com")

        assert attempts(bob)["attempts"] == []
        assert bob.get(f"/publishing/attempts/{alice_attempt['id']}").status_code == 404
        # The key is not a capability: Bob cannot use it against Alice's post.
        assert publish_ready(bob, post["id"], key="alice-publish-1").status_code == 404
        assert provider.publish_calls == 1


# ── An unreadable credential is a reconnect, never a resend ──────────────


def break_credential(store, account_id, field="access_token"):
    """Simulate a rotated TOKEN_ENCRYPTION_KEY or a DB restored under another key."""
    with store.connection() as db:
        db.execute(f"UPDATE accounts SET {field}='not-a-fernet-token' WHERE id=?", (account_id,))


def test_an_unreadable_credential_offers_reconnect_not_a_resend(settings):
    provider = Instagram()
    app = make_app(settings, provider)
    with TestClient(app) as client:
        signup(client)
        account = connect(client)
        break_credential(app.state.store, account["id"])

        response = create_instagram_post(client, account, key="unreadable-key-1")

        assert response.status_code == 409
        assert "Reconnect" in response.json()["detail"]
        assert provider.create_calls == 0
        row = attempts(client)["attempts"][0]
        assert row["status"] == "failed"
        # A resend with the same dead credential would fail identically forever.
        assert row["next_action"] == "reconnect"
        assert row["retry_action"] == {
            "kind": "reconnect", "endpoint": None, "key": "none", "retry_at": None,
        }
        # The failure is accurate: nothing was dispatched.
        assert "credentials" in row["error"].lower()


def test_an_unreadable_credential_blocks_upload_with_reconnect(settings):
    provider = YouTube()
    app = make_app(settings, provider)
    with TestClient(app) as client:
        signup(client)
        account = connect(client, "youtube")
        break_credential(app.state.store, account["id"])

        response = upload_youtube(client, account, key="unreadable-key-2")

        assert response.status_code == 409
        assert provider.uploads == 0
        row = attempts(client)["attempts"][0]
        assert row["retry_action"]["kind"] == "reconnect"


def test_an_unreadable_credential_blocks_publish_with_reconnect(settings):
    provider = Instagram()
    app = make_app(settings, provider)
    with TestClient(app) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        break_credential(app.state.store, account["id"])

        response = publish_ready(client, post["id"], key="unreadable-pub-1")

        assert response.status_code == 409
        assert "Reconnect" in response.json()["detail"]
        assert provider.publish_calls == 0
        # The token is read *before* the claim, so nothing was dispatched and the
        # job is not marked failed: it keeps its real state and stays publishable
        # after a reconnect. The receipt offers no retry at all.
        row = attempts(client)["attempts"][0]
        assert row["status"] == "processing"
        assert row["retry_action"] is None
        assert row["next_action"] not in {"retry", "retry_later"}
        scope = app.state.store.workspace.set(client.get("/session").json()["workspace"]["id"])
        try:
            assert app.state.store.get_post(post["id"])["status"] == "processing"
        finally:
            app.state.store.workspace.reset(scope)
        # And /accounts says why the channel cannot be used right now.
        assert client.get("/accounts").json()["accounts"][0]["credentials_readable"] is False


def test_reconnecting_clears_the_reconnect_refusal_and_the_retry_works(settings):
    provider = Instagram()
    app = make_app(settings, provider)
    with TestClient(app) as client:
        signup(client)
        account = connect(client)
        break_credential(app.state.store, account["id"])
        assert create_instagram_post(client, account, key="reconnect-key-1").status_code == 409
        assert attempts(client)["attempts"][0]["next_action"] == "reconnect"

        # Reconnecting is the advertised remedy; it must actually re-open the retry.
        connect(client)
        row = attempts(client)["attempts"][0]
        assert row["next_action"] == "retry"
        assert row["retry_action"]["kind"] == "resend"

        retried = create_instagram_post(client, account, key="reconnect-key-1")
        assert retried.status_code == 201, retried.text
        assert provider.create_calls == 1
        assert len(attempts(client, status="all")["attempts"]) == 1


def test_a_legacy_create_receipt_self_heals_on_the_next_attempt(settings):
    """A row written before the reconnect classification must stop lying.

    Create/upload read the token *after* re-arming the receipt, so the retry
    itself rewrites the refusal correctly. No migration and no message matching.
    """
    provider = Instagram()
    app = make_app(settings, provider)
    with TestClient(app) as client:
        signup(client)
        account = connect(client)
        store = app.state.store
        scope = store.workspace.set(client.get("/session").json()["workspace"]["id"])
        try:
            legacy = store.create_attempt(
                account_id=account["id"],
                provider="instagram",
                status="failed",
                error="Stored credentials can't be read. Reconnect this account.",
                retry_kind="none",
                idempotency_key="legacy-key-0001",
                request_fingerprint=fingerprint(
                    {
                        "operation": "instagram.create",
                        "account_id": account["id"],
                        "media_type": "IMAGE",
                        "media_url": "https://cdn.example/media",
                        "caption": "",
                    }
                ),
                media_type="IMAGE",
                media_url="https://cdn.example/media",
            )
        finally:
            store.workspace.reset(scope)
        # Pre-fix shape: it advertises a resend.
        assert attempts(client)["attempts"][0]["retry_action"]["kind"] == "resend"

        break_credential(store, account["id"])
        # The retry must be the same logical request (same caption) or the key is
        # correctly refused as reused before the credential is ever read.
        response = create_instagram_post(client, account, key="legacy-key-0001", caption="")
        assert response.status_code == 409
        assert provider.create_calls == 0
        row = attempts(client)["attempts"][0]
        assert row["id"] == legacy["id"]
        assert row["retry_action"]["kind"] == "reconnect"


def test_a_legacy_publish_receipt_is_repaired_even_though_it_cannot_self_heal(settings):
    """The publish path reads the token before touching the receipt.

    That ordering is deliberate (a local failure must not consume the claim), but
    it means a pre-fix receipt would keep advertising a resend forever. The route
    repairs the stale refusal at the one point where it is provably still true.

    A pre-fix receipt also has no ``failure_stage``, so it cannot prove its
    container was ever publishable. It is therefore failed-safe: it advertises a
    new attempt, not a resend that could re-arm a dead container.
    """
    provider = Instagram()
    app = make_app(settings, provider)
    with TestClient(app) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        store = app.state.store
        scope = store.workspace.set(client.get("/session").json()["workspace"]["id"])
        try:
            attempt = store.attempt_for_post(post["id"])
            store.update_attempt(
                attempt["id"],
                status="failed",
                retry_kind="none",
                error="Stored credentials can't be read. Reconnect this account.",
            )
            db = store.connection()
            with db as conn:
                conn.execute("UPDATE instagram_posts SET status='failed' WHERE id=?", (post["id"],))
        finally:
            store.workspace.reset(scope)
        # No provenance: never assume the container is reusable.
        assert attempts(client)["attempts"][0]["retry_action"]["kind"] == "new_attempt"

        break_credential(store, account["id"])
        response = publish_ready(client, post["id"], key="legacy-publish-1")

        assert response.status_code == 409
        assert provider.publish_calls == 0
        row = attempts(client)["attempts"][0]
        assert row["status"] == "failed"
        assert row["next_action"] == "reconnect"
        assert row["retry_action"]["kind"] == "reconnect"
        # The job itself was not consumed: reconnecting can still publish it.
        scope = store.workspace.set(client.get("/session").json()["workspace"]["id"])
        try:
            assert store.get_post(post["id"])["status"] == "failed"
        finally:
            store.workspace.reset(scope)


def test_accounts_report_whether_credentials_are_readable(settings):
    provider = Instagram()
    app = make_app(settings, provider)
    with TestClient(app) as client:
        signup(client)
        healthy = connect(client)
        store = app.state.store
        scope = store.workspace.set(client.get("/session").json()["workspace"]["id"])
        try:
            broken = store.save_account(
                "instagram", "second-professional", "Second",
                {"access_token": "ig-2", "expires_in": 5184000}, "scope",
            )
        finally:
            store.workspace.reset(scope)
        break_credential(store, broken["id"])

        rows = {
            row["id"]: row
            for row in client.get("/accounts").json()["accounts"]
        }

        assert rows[healthy["id"]]["credentials_readable"] is True
        assert rows[broken["id"]]["credentials_readable"] is False
        # Only a boolean is reported; no token material is ever returned.
        text = client.get("/accounts").text
        assert "not-a-fernet-token" not in text
        assert "access_token" not in text


def test_a_healthy_account_still_reports_readable_after_a_failed_refresh(settings):
    app = make_app(settings, lambda request: httpx.Response(500))
    with TestClient(app) as client:
        store = app.state.store
        account = store.save_account(
            "youtube", "chan", "Channel",
            {"access_token": "a", "refresh_token": "r", "expires_in": 3600}, "scope",
        )
        assert client.get("/accounts", headers=HEADERS).json()["accounts"][0][
            "credentials_readable"
        ] is True
        # A refresh that cannot reach the provider must not change that fact.
        assert client.post(
            f"/accounts/{account['id']}/refresh", headers=HEADERS
        ).status_code == 502
        assert client.get("/accounts", headers=HEADERS).json()["accounts"][0][
            "credentials_readable"
        ] is True


# ── Byte-bound YouTube idempotency ───────────────────────────────────────


def test_same_size_different_bytes_are_not_the_same_upload(settings):
    """Filename, size, and MIME are not an identity; the bytes are."""
    provider = YouTube()
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client, "youtube")
        first = upload_youtube(client, account, key="bytes-key-0001", content=b"aaaa")
        assert first.status_code == 201, first.text

        changed = upload_youtube(client, account, key="bytes-key-0001", content=b"bbbb")

        assert changed.status_code == 409
        assert "different upload" in changed.json()["detail"]
        assert provider.uploads == 1
        # One logical submission, one ledger row, still the first receipt.
        rows = attempts(client, status="all")["attempts"]
        assert len(rows) == 1
        assert rows[0]["request_key"] == "bytes-key-0001"


def test_identical_bytes_with_the_same_key_replay_the_receipt(settings):
    provider = YouTube()
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client, "youtube")
        first = upload_youtube(client, account, key="bytes-key-0002", content=b"same-bytes")
        second = upload_youtube(client, account, key="bytes-key-0002", content=b"same-bytes")

        assert first.status_code == second.status_code == 201
        assert second.json()["attempt"]["id"] == first.json()["attempt"]["id"]
        assert provider.uploads == 1


def test_a_changed_file_under_a_new_key_is_a_new_upload(settings):
    provider = YouTube()
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client, "youtube")
        assert upload_youtube(client, account, key="bytes-key-0003", content=b"aaaa").status_code == 201
        assert upload_youtube(client, account, key="bytes-key-0004", content=b"bbbb").status_code == 201
        assert provider.uploads == 2
        assert len(attempts(client, status="all")["attempts"]) == 2


# ── Simultaneous retries and unpersisted claims ──────────────────────────


def test_simultaneous_manual_retries_of_one_key_dispatch_once(settings):
    """Two clicks on the same retry must not create two containers."""
    import asyncio

    provider = Instagram()
    provider.create_response = httpx.Response(400, json={"error": {"message": "bad"}})
    app = make_app(settings, provider)
    with TestClient(app) as client:
        account = app.state.store.save_account(
            "instagram", "professional-id", "IG",
            {"access_token": "ig-long", "expires_in": 5184000}, "scope",
        )
        first = client.post(
            "/instagram/posts",
            headers=HEADERS | {"Idempotency-Key": "retry-race-01"},
            json={
                "account_id": account["id"],
                "media_type": "IMAGE",
                "media_url": "https://cdn.example/media",
                "caption": "Hello",
            },
        )
        assert first.status_code == 502, first.text

    provider.create_response = None
    app = make_app(settings, provider)

    async def scenario():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                entered, release = asyncio.Event(), asyncio.Event()

                async def slow_create(*args, **kwargs):
                    entered.set()
                    await release.wait()
                    return "container-retry"

                app.state.providers.create_instagram_post = slow_create
                payload = {
                    "account_id": account["id"],
                    "media_type": "IMAGE",
                    "media_url": "https://cdn.example/media",
                    "caption": "Hello",
                }
                headers = HEADERS | {"Idempotency-Key": "retry-race-01"}
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

    assert {first.status_code, second.status_code} <= {201, 202, 409}
    # Exactly one provider dispatch for one logical submission.
    assert provider.create_calls == 1
    with sqlite3.connect(settings.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM publishing_attempts").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM instagram_posts").fetchone()[0] == 1


def test_an_unwritable_claim_blocks_dispatch(settings):
    """No durable claim, no external call — for the retry path too."""
    provider = Instagram()
    app = make_app(settings, provider)
    with TestClient(app) as client:
        signup(client)
        account = connect(client)
        store = app.state.store
        original = store.claim_attempt

        def refuse(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        store.claim_attempt = refuse
        try:
            response = create_instagram_post(client, account, key="claim-refused-1")
        finally:
            store.claim_attempt = original

        # A claim that cannot be persisted is a 503 for THIS request, never a
        # dispatch: the wording says nothing about any earlier attempt.
        assert response.status_code == 503
        assert "Nothing was sent for this request" in response.json()["detail"]
        assert provider.create_calls == 0
        # The receipt exists but was never dispatched, so it says exactly that.
        row = attempts(client)["attempts"][0]
        assert row["status"] == "pending"
        assert row["next_action"] == "wait"


# ── Success is only reported when it is durable ──────────────────────────


def test_a_publish_that_cannot_be_recorded_is_not_reported_as_success(settings):
    provider = Instagram()
    app = make_app(settings, provider)
    with TestClient(app) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        store = app.state.store
        original = store.update_post

        def refuse_published(post_id, status, media_id=None):
            if status == "published":
                raise StorageError("The cloud database is temporarily unavailable.",
                                   outcome="not_applied")
            return original(post_id, status, media_id)

        store.update_post = refuse_published
        try:
            response = publish_ready(client, post["id"])
        finally:
            store.update_post = original

        # Instagram published. Reporting a plain failure would invite a second
        # publish; reporting success would hide that the record is missing.
        assert response.status_code == 503
        assert response.json()["outcome"] == "unknown"
        assert "Instagram accepted the publish" in response.json()["detail"]
        assert provider.publish_calls == 1
        # The job is still claimed, so no later request can publish it again.
        scope = store.workspace.set(client.get("/session").json()["workspace"]["id"])
        try:
            assert store.get_post(post["id"])["status"] == "publishing"
        finally:
            store.workspace.reset(scope)
        assert publish_ready(client, post["id"]).status_code == 409
        assert provider.publish_calls == 1


def test_an_upload_that_cannot_be_recorded_is_not_reported_as_success(settings):
    provider = YouTube()
    app = make_app(settings, provider)
    with TestClient(app) as client:
        signup(client)
        account = connect(client, "youtube")
        store = app.state.store
        original = store.update_attempt

        def refuse_published(attempt_id, **fields):
            if fields.get("status") == "published":
                raise StorageError("The cloud database is temporarily unavailable.",
                                   outcome="not_applied")
            return original(attempt_id, **fields)

        store.update_attempt = refuse_published
        try:
            response = upload_youtube(client, account)
        finally:
            store.update_attempt = original

        assert response.status_code == 503
        assert response.json()["outcome"] == "unknown"
        assert "YouTube accepted the upload" in response.json()["detail"]
        assert provider.uploads == 1
        # The receipt still says a call was dispatched, so it is never re-sent.
        row = attempts(client)["attempts"][0]
        assert row["status"] == "submitted"
        assert row["next_action"] == "check"


# ── Runtime initialization bypass is gone ────────────────────────────────


def test_the_store_has_no_runtime_initialization_parameter(settings):
    with pytest.raises(TypeError):
        Store(settings.database_path, settings.encryption_key, database_init="create")


def test_a_local_store_still_creates_its_own_file(settings):
    """Fail-closed applies to a configured cloud bucket, not to local SQLite."""
    store = Store(settings.database_path, settings.encryption_key)
    assert settings.database_path.exists()
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM publishing_attempts").fetchone()[0] == 0


def test_an_initialized_snapshot_boots_and_serves_writes(settings, monkeypatch):
    """The one-shot init must produce a snapshot the service can actually use."""
    from google.cloud import storage
    from test_recovery_hardening import Blob

    blob = Blob()
    monkeypatch.setenv("DATABASE_BUCKET", "test-private-bucket")
    monkeypatch.setattr(storage, "Client", lambda: type("Client", (), {
        "bucket": lambda self, name: type("Bucket", (), {"blob": lambda self, name: blob})(),
    })())

    assert Store.initialize_cloud_snapshot(settings.database_path, settings.encryption_key)

    # The uploaded object is one self-contained SQLite file with the full schema.
    assert blob.data.startswith(b"SQLite format 3\x00")
    assert not settings.database_path.with_name(
        f"{settings.database_path.name}.init"
    ).exists()

    store = Store(settings.database_path, settings.encryption_key)
    account = store.save_account(
        "youtube", "chan", "Channel", {"access_token": "t", "expires_in": 3600}, "scope"
    )
    assert store.list_accounts()[0]["id"] == account["id"]
    # The write was uploaded, so a fresh instance sees it.
    settings.database_path.unlink()
    assert Store(settings.database_path, settings.encryption_key).list_accounts()[0][
        "external_id"
    ] == "chan"


def test_the_init_script_reports_and_never_replaces(settings, monkeypatch, tmp_path, capsys):
    """The operator command is create-if-absent and says which happened."""
    import importlib.util
    from pathlib import Path

    from google.cloud import storage
    from test_recovery_hardening import Blob

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "init_cloud_database", root / "scripts" / "init_cloud_database.py"
    )
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)

    blob = Blob()
    monkeypatch.setenv("DATABASE_BUCKET", "test-private-bucket")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", settings.encryption_key)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "init.sqlite3"))
    monkeypatch.setattr(storage, "Client", lambda: type("Client", (), {
        "bucket": lambda self, name: type("Bucket", (), {"blob": lambda self, name: blob})(),
    })())

    assert script.main() == 0
    assert "Created" in capsys.readouterr().out
    first = blob.data
    generation = blob.generation

    # A second run changes nothing at all.
    assert script.main() == 0
    assert "already exists" in capsys.readouterr().out
    assert blob.data == first
    assert blob.generation == generation

    monkeypatch.delenv("DATABASE_BUCKET")
    assert script.main() == 2
    assert "DATABASE_BUCKET is not set" in capsys.readouterr().err


def test_migration_preserves_existing_rows_and_keys(settings):
    """Opening an existing local database must not rewrite or drop receipts."""
    store = Store(settings.database_path, settings.encryption_key)
    account = store.save_account(
        "youtube", "chan", "Channel", {"access_token": "t", "expires_in": 3600}, "scope"
    )
    created = store.create_attempt(
        account_id=account["id"], provider="youtube", status="failed",
        idempotency_key="kept-key-0001",
    )
    reopened = Store(settings.database_path, settings.encryption_key)
    with reopened.connection() as db:
        row = dict(db.execute(
            "SELECT * FROM publishing_attempts WHERE id=?", (created["id"],)
        ).fetchone())
    assert row["idempotency_key"] == "kept-key-0001"
    assert row["status"] == "failed"
    assert reopened.public_attempt(row)["request_key"] == "kept-key-0001"
    with sqlite3.connect(settings.database_path) as db:
        assert list(db.execute("PRAGMA foreign_key_check")) == []
