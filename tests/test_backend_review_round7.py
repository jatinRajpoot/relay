"""Round 7 independent backend review: confirmed defects and guards.

Each test below was written as a *reproducible* defect found by reading the
finished source, not by trusting the author's report. They were ``xfail(strict=True)``
until the minimum fix in each docstring was applied; the markers are now removed
and the original failure assertions are unchanged, so they are ordinary
regressions.

The two non-defect tests are guards for behaviour that already held: a stale
sweep cannot erase a provider-confirmed success, and it cannot let a manual retry
steal an in-flight claim.
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

# ── Defect 1: a 5xx must never be classified as a definite non-effect ────


class _QuotaThenSuccess(YouTube):
    """A 5xx whose body happens to carry a quota reason, then a success."""

    def __init__(self):
        super().__init__()
        self.puts = 0

    def __call__(self, request):
        if request.url.path == "/upload/youtube/v3/videos" and request.method == "PUT":
            self.puts += 1
            if self.puts == 1:
                # A gateway/edge 5xx that echoed an upstream quota reason. The
                # upload may already have created a video; only the status code
                # can prove ambiguity, and it says 5xx.
                return httpx.Response(
                    500, json={"error": {"errors": [{"reason": "rateLimitExceeded"}]}}
                )
            return httpx.Response(
                201, json={"id": f"video-{self.puts}", "status": {"privacyStatus": "private"}}
            )
        return super().__call__(request)


def test_a_5xx_with_a_quota_body_is_not_a_definite_non_effect(settings):
    """A 5xx carrying a quota-shaped body is still ambiguous, never a resend.

    Regression for ``ProviderCore.request``: it used to read the error body for
    auth/rate-limit *before* the status code, so a gateway 5xx echoing an
    upstream quota reason became ``rate_limited`` (a definite non-effect). The
    receipt then advertised a resend and a second upload was dispatched after
    the cooldown — a duplicate video. Ambiguity is now decided from the status
    line first (408/5xx/redirect), so the body can only classify a definite 4xx.
    """
    provider = _QuotaThenSuccess()
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client, "youtube")

        first = upload_youtube(client, account, key="review-5xx-quota-01")

        # The provider may have created the video. The only honest answer is
        # "unknown", never a retryable refusal.
        assert first.status_code == 502, first.text
        row = attempts(client, status="all")["attempts"][0]
        assert row["status"] == "unknown"
        assert row["retry_action"] is None
        assert provider.puts == 1


# ── Defect 2: a dead Instagram container is not resendable ───────────────


def test_a_dead_container_is_not_advertised_as_a_resendable_publish(settings):
    """A container-processing ERROR must offer a new attempt, never a resend.

    Regression for the ambiguous ``failed`` Instagram job: ``sync_post`` mapped a
    container ERROR to job ``failed`` and ``_retry_action`` treated any failed job
    as a reusable container, so the receipt advertised a resend to ``/publish``.
    Following it re-armed the job and dispatched a publish the provider rejects
    forever. Failures now carry explicit provenance (``failure_stage``):
    ``container`` here, ``publish`` only for a refused publish call. The route
    enforces the same rule, so a direct POST cannot bypass the projection.
    """
    provider = Instagram()
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()

        provider.status_code = "ERROR"
        assert client.get(f"/instagram/posts/{post['id']}").json()["status"] == "failed"

        row = attempts(client)["attempts"][0]
        assert row["retry_action"]["kind"] == "new_attempt", row["retry_action"]


# ── Defect 3: an expired Instagram token is a reconnect, not a resend ────


def test_an_expired_instagram_token_does_not_leave_a_resend_recipe(settings):
    """An expired Instagram credential is a reconnect, not a resend.

    Regression: the route repaired a receipt to ``reconnect`` only for
    ``CredentialsUnreadable``. An *expired* token raises a plain 409 from
    ``valid_account``, so the failed receipt kept advertising a resend that the
    endpoint answers with 409 "Instagram token expired". ``_retry_action`` now
    reads the account's ``expires_at`` (a timestamp only — never the ciphertext)
    and offers ``reconnect`` for an expired Instagram credential. YouTube is
    excluded because its access token is refreshed automatically.
    """
    provider = Instagram()
    provider.create_response = httpx.Response(400, json={"error": {"message": "bad"}})
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        assert create_instagram_post(client, account, key="review-expire-01").status_code == 502

        with client.app.state.store.connection() as db:
            db.execute("UPDATE accounts SET expires_at=?", (int(time.time()) - 10,))

        row = attempts(client)["attempts"][0]
        assert row["retry_action"]["kind"] == "reconnect", row["retry_action"]


# ── Defect 4: a local SQLite failure after a confirmed publish ───────────


def test_a_local_sqlite_failure_after_a_confirmed_publish_is_a_sanitised_unknown(settings):
    """A local SQLite error after a confirmed publish is a sanitised 503/unknown.

    Regression: the route caught only ``StorageError`` around the post-success
    ``update_post``. A local ``sqlite3.Error`` (disk full/locked/I/O) escaped as a
    bare 500 with no ``outcome``, so the client was never told the post may be
    live. The post-dispatch persistence boundary now catches both. The claim is
    preserved (no duplicate), and a programming error is still not swallowed.
    """
    provider = Instagram()
    app = make_app(settings, provider)
    with TestClient(app, raise_server_exceptions=False) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()

        store = app.state.store
        original = store.update_post

        def refuse(post_id, status, media_id=None):
            if status == "published":
                raise sqlite3.OperationalError("disk I/O error")
            return original(post_id, status, media_id)

        store.update_post = refuse
        try:
            response = publish_ready(client, post["id"])
        finally:
            store.update_post = original

        assert response.status_code == 503, response.text
        assert response.json()["outcome"] == "unknown"
        assert provider.publish_calls == 1


# ── Guards: the sweep is database-only and cannot erase a known success ──


def test_a_stale_sweep_then_a_known_success_is_recovered(settings):
    provider = Instagram()
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        store = app_store = client.app.state.store

        with app_store.connection() as db:
            db.execute("UPDATE instagram_posts SET status='ready' WHERE id=?", (post["id"],))
        assert store.claim_post(post["id"])
        with store.connection() as db:
            db.execute(
                "UPDATE publishing_attempts SET claimed_at=? WHERE post_id=?",
                (int(time.time()) - 400, post["id"]),
            )

        # Listing ages the claim out; it must never dispatch.
        assert attempts(client)["attempts"][0]["status"] == "unknown"
        assert provider.publish_calls == 0

        # The provider then confirms the publish. A status read must record it,
        # not keep the sweep's guess.
        provider.status_code = "PUBLISHED"
        assert client.get(f"/instagram/posts/{post['id']}").json()["status"] == "published"
        assert attempts(client, status="all")["attempts"][0]["status"] == "published"
        assert provider.publish_calls == 0


def test_a_manual_retry_cannot_steal_an_aged_in_flight_claim(settings):
    provider = Instagram()
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        store = client.app.state.store

        with store.connection() as db:
            db.execute("UPDATE instagram_posts SET status='ready' WHERE id=?", (post["id"],))
        assert store.claim_post(post["id"], "review-publish-01")
        with store.connection() as db:
            db.execute(
                "UPDATE publishing_attempts SET claimed_at=? WHERE post_id=?",
                (int(time.time()) - 400, post["id"]),
            )
        assert attempts(client)["attempts"][0]["status"] == "unknown"

        # A manual retry after the sweep must answer from the ledger, not
        # dispatch a second publish for a claim the provider may still be serving.
        response = publish_ready(client, post["id"], key="review-publish-01")
        assert response.status_code in {200, 202, 409}
        assert provider.publish_calls == 0


# ── Fix 1 matrix: conflicting HTTP/body signals stay ambiguous ───────────
#
# The status line decides ambiguity before the body is read. Each case below
# pairs an ambiguous status with a body that *would* have been classified as a
# definite non-effect under the old body-first ordering. None may dispatch twice.


class _CountingYouTube(YouTube):
    def __init__(self, response):
        super().__init__()
        self.response = response
        self.puts = 0

    def __call__(self, request):
        if request.url.path == "/upload/youtube/v3/videos" and request.method == "PUT":
            self.puts += 1
            return self.response
        return super().__call__(request)


class _CountingInstagram(Instagram):
    def __init__(self, response):
        super().__init__()
        self.response = response

    def __call__(self, request):
        if request.url.path == "/v25.0/professional-id/media":
            self.create_calls += 1
            return self.response
        return super().__call__(request)


_AMBIGUOUS_WITH_DEFINITE_BODY = [
    (500, {"error": {"errors": [{"reason": "rateLimitExceeded"}]}}, "yt-500-quota"),
    (503, {"error": {"code": 190, "message": "expired"}}, "yt-503-auth"),
    (502, {"error": {"errors": [{"reason": "quotaExceeded"}]}}, "yt-502-quota"),
    (504, {"error": {"code": 4, "message": "rate"}}, "yt-504-meta-rate"),
    (408, {"error": {"errors": [{"reason": "rateLimitExceeded"}]}}, "yt-408-quota"),
    (302, {"error": {"code": 190}}, "yt-redirect-auth"),
]


@pytest.mark.parametrize(
    "status,body,label", _AMBIGUOUS_WITH_DEFINITE_BODY, ids=[c[2] for c in _AMBIGUOUS_WITH_DEFINITE_BODY]
)
def test_a_youtube_ambiguous_status_ignores_a_definite_body(settings, status, body, label):
    provider = _CountingYouTube(httpx.Response(status, json=body))
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client, "youtube")
        response = upload_youtube(client, account, key=f"matrix-{label}")

        assert response.status_code == 502, response.text
        row = attempts(client)["attempts"][0]
        assert row["status"] == "unknown", row
        assert row["retry_action"] is None
        # The receipt offers no retry, so nothing can dispatch a second upload.
        assert provider.puts == 1
        # A keyed repeat answers from the ledger and never re-uploads.
        assert upload_youtube(client, account, key=f"matrix-{label}").status_code == 202
        assert provider.puts == 1


_AMBIGUOUS_IG = [
    (500, {"error": {"errors": [{"reason": "rateLimitExceeded"}]}}, "ig-500-quota"),
    (500, {"error": {"code": 190, "message": "expired"}}, "ig-500-auth"),
    (503, {"error": {"code": 4, "message": "rate"}}, "ig-503-meta-rate"),
    (408, {"error": {"code": 190}}, "ig-408-auth"),
    (307, {"error": {"errors": [{"reason": "quotaExceeded"}]}}, "ig-redirect-quota"),
]


@pytest.mark.parametrize(
    "status,body,label", _AMBIGUOUS_IG, ids=[c[2] for c in _AMBIGUOUS_IG]
)
def test_an_instagram_ambiguous_status_ignores_a_definite_body(settings, status, body, label):
    provider = _CountingInstagram(httpx.Response(status, json=body))
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        response = create_instagram_post(client, account, key=f"matrix-{label}")

        assert response.status_code == 502, response.text
        row = attempts(client)["attempts"][0]
        assert row["status"] == "unknown", row
        assert row["retry_action"] is None
        assert provider.create_calls == 1
        assert create_instagram_post(client, account, key=f"matrix-{label}").status_code == 202
        assert provider.create_calls == 1


def test_a_definite_4xx_body_still_classifies_rate_and_auth(settings):
    """The fix must not flatten legitimate 4xx classification into ambiguity."""
    # Meta reports a rate limit as HTTP 400 with a known code: still a wait.
    provider = Instagram()
    provider.create_response = httpx.Response(400, json={"error": {"code": 4, "message": "rate"}})
    with TestClient(make_app(settings, provider)) as client:
        signup(client, "rate@example.com")
        account = connect(client)
        assert create_instagram_post(client, account).status_code == 429
        row = attempts(client)["attempts"][0]
        assert row["status"] == "failed"
        assert row["retry_kind"] == "delay"
        assert row["next_action"] == "retry_later"

    # Google reports quota as HTTP 403: still a wait.
    youtube = YouTube()
    youtube.upload_response = httpx.Response(
        403, json={"error": {"errors": [{"reason": "quotaExceeded"}]}}
    )
    with TestClient(make_app(settings, youtube)) as client:
        signup(client, "quota@example.com")
        account = connect(client, "youtube")
        assert upload_youtube(client, account).status_code == 429
        row = attempts(client)["attempts"][0]
        assert row["status"] == "failed"
        assert row["retry_kind"] == "delay"

    # A definite 4xx auth body is still a reconnect.
    instagram = Instagram()
    instagram.create_response = httpx.Response(400, json={"error": {"code": 190}})
    with TestClient(make_app(settings, instagram)) as client:
        signup(client, "auth@example.com")
        account = connect(client)
        assert create_instagram_post(client, account).status_code == 409
        row = attempts(client)["attempts"][0]
        assert row["retry_kind"] == "reconnect"


# ── Fix 2: a dead container cannot be re-armed by any endpoint ───────────


def test_a_dead_container_cannot_be_rearmed_by_a_direct_publish(settings):
    """The projection is not the only guard: the endpoint must refuse too."""
    provider = Instagram()
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()

        provider.status_code = "ERROR"
        assert client.get(f"/instagram/posts/{post['id']}").json()["status"] == "failed"
        row = attempts(client)["attempts"][0]
        assert row["retry_action"]["kind"] == "new_attempt"
        # The receipt explains the media problem in words the user can act on.
        assert "process this media" in row["error"]

        # Following the *old* resend shape directly (keyed and keyless) must not
        # re-arm the container or reach the provider.
        assert publish_ready(client, post["id"], key="dead-container-1").status_code == 409
        assert publish_ready(client, post["id"]).status_code == 409
        assert provider.publish_calls == 0
        # The job is untouched, so a later status read still reports the truth.
        assert client.get(f"/instagram/posts/{post['id']}").json()["status"] == "failed"


def test_a_legacy_failed_container_without_provenance_is_failed_safe(settings):
    """An old ``failed`` row with no stage must never be assumed reusable."""
    provider = Instagram()
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        store = client.app.state.store
        with store.connection() as db:
            db.execute("UPDATE instagram_posts SET status='failed' WHERE id=?", (post["id"],))
            db.execute(
                "UPDATE publishing_attempts SET status='failed', retry_kind='none', "
                "failure_stage=NULL WHERE post_id=?",
                (post["id"],),
            )

        row = attempts(client)["attempts"][0]
        assert row["failure_stage"] is None
        assert row["retry_action"]["kind"] == "new_attempt"
        assert publish_ready(client, post["id"]).status_code == 409
        assert provider.publish_calls == 0


def test_a_publish_refusal_is_still_resendable_and_keeps_its_provenance(settings):
    """The fix must not make a genuine publish refusal un-retryable."""
    provider = Instagram()
    provider.publish_response = httpx.Response(400, json={"error": {"message": "bad"}})
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        assert publish_ready(client, post["id"], key="refusal-keep-1").status_code == 502

        row = attempts(client)["attempts"][0]
        assert row["failure_stage"] == "publish"
        assert row["retry_action"]["kind"] == "resend"

        provider.publish_response = None
        retried = publish_ready(client, post["id"], key="refusal-keep-1")
        assert retried.status_code == 200, retried.text
        assert provider.publish_calls == 2
        assert len(attempts(client, status="all")["attempts"]) == 1


def test_a_container_that_dies_after_a_publish_refusal_is_no_longer_resendable(settings):
    """Newer provenance must replace older provenance, not be coalesced away."""
    provider = Instagram()
    # A rate limit is a definite non-effect: the job returns to "ready" and the
    # receipt records a publish-stage refusal with a cooldown.
    provider.publish_response = httpx.Response(429, headers={"Retry-After": "30"})
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        assert publish_ready(client, post["id"]).status_code == 429
        row = attempts(client)["attempts"][0]
        assert row["failure_stage"] == "publish"
        assert row["retry_action"]["kind"] == "wait"

        # The container then dies before the cooldown ends. The status read is the
        # newer, authoritative fact: it must replace the publish provenance and
        # clear the now-meaningless cooldown.
        provider.status_code = "ERROR"
        assert client.get(f"/instagram/posts/{post['id']}").json()["status"] == "failed"
        row = attempts(client)["attempts"][0]
        assert row["failure_stage"] == "container"
        assert row["retry_kind"] == "none"
        assert row["retry_at"] is None
        assert row["retry_action"]["kind"] == "new_attempt"
        assert publish_ready(client, post["id"]).status_code == 409
        assert provider.publish_calls == 1


# ── Fix 3: expired Instagram credential is a reconnect ───────────────────


def test_an_expired_instagram_credential_is_a_reconnect_on_both_recipes(settings):
    """Create and publish recipes must agree with the endpoint after expiry."""
    provider = Instagram()
    provider.publish_response = httpx.Response(400, json={"error": {"message": "bad"}})
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        assert publish_ready(client, post["id"], key="expire-pub-1").status_code == 502
        with client.app.state.store.connection() as db:
            db.execute("UPDATE accounts SET expires_at=?", (int(time.time()) - 10,))

        row = attempts(client)["attempts"][0]
        assert row["retry_action"]["kind"] == "reconnect", row
        assert row["next_action"] == "reconnect"
        # The endpoint agrees with the recipe: it refuses rather than dispatching.
        assert create_instagram_post(client, account, key="expire-create-1").status_code == 409
        assert publish_ready(client, post["id"], key="expire-pub-1").status_code == 409
        assert provider.create_calls == 1
        assert provider.publish_calls == 1

        # Reconnecting restores the correct action without sending anything.
        connect(client)
        row = attempts(client)["attempts"][0]
        assert row["retry_action"]["kind"] == "resend"
        assert row["next_action"] == "retry"
        assert provider.create_calls == 1
        assert provider.publish_calls == 1


def test_an_expired_youtube_credential_is_not_forced_into_a_reconnect(settings):
    """YouTube refreshes automatically, so expiry alone is not a reconnect."""
    provider = YouTube()
    provider.upload_response = httpx.Response(400, json={"error": {"message": "invalid"}})
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client, "youtube")
        assert upload_youtube(client, account, key="yt-expiry-01").status_code == 502
        with client.app.state.store.connection() as db:
            db.execute("UPDATE accounts SET expires_at=?", (int(time.time()) - 10,))

        row = attempts(client)["attempts"][0]
        assert row["retry_action"]["kind"] == "resend"
        assert row["next_action"] == "retry"


def test_an_expiry_during_a_cooldown_agrees_with_the_endpoint(settings):
    """A recorded cooldown must not outrank a credential that has since expired.

    The projection and the endpoint use the same predicate, so the recipe the
    client was shown (``reconnect``) is exactly what the endpoint answers (409),
    not a 429 for a cooldown the recipe no longer mentions.
    """
    provider = Instagram()
    provider.publish_response = httpx.Response(429, headers={"Retry-After": "120"})
    with TestClient(make_app(settings, provider)) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        assert publish_ready(client, post["id"], key="cool-expire-1").status_code == 429
        assert attempts(client)["attempts"][0]["retry_action"]["kind"] == "wait"

        # The credential expires while the cooldown is still running.
        with client.app.state.store.connection() as db:
            db.execute("UPDATE accounts SET expires_at=?", (int(time.time()) - 10,))

        row = attempts(client)["attempts"][0]
        assert row["retry_action"]["kind"] == "reconnect", row
        assert row["next_action"] == "reconnect"
        # The endpoint agrees: reconnect, not a cooldown 429.
        assert publish_ready(client, post["id"], key="cool-expire-1").status_code == 409
        assert publish_ready(client, post["id"]).status_code == 409
        assert provider.publish_calls == 1


# ── Fix 4: post-dispatch persistence failures on both providers ──────────


def test_a_youtube_sqlite_failure_after_a_confirmed_upload_is_a_sanitised_unknown(settings):
    provider = YouTube()
    app = make_app(settings, provider)
    with TestClient(app, raise_server_exceptions=False) as client:
        signup(client)
        account = connect(client, "youtube")
        store = app.state.store
        original = store.update_attempt

        def refuse(attempt_id, **fields):
            if fields.get("status") == "published":
                raise sqlite3.OperationalError("database is locked")
            return original(attempt_id, **fields)

        store.update_attempt = refuse
        try:
            response = upload_youtube(client, account)
        finally:
            store.update_attempt = original

        assert response.status_code == 503, response.text
        assert response.json()["outcome"] == "unknown"
        assert "YouTube accepted the upload" in response.json()["detail"]
        # No raw SQL, token, or disk detail leaks.
        assert "sqlite" not in response.text.lower()
        assert "locked" not in response.text.lower()
        assert provider.uploads == 1
        # The claim is preserved, so the receipt is never re-sent.
        row = attempts(client)["attempts"][0]
        assert row["status"] == "submitted"
        assert row["next_action"] == "check"


def test_an_instagram_sqlite_failure_after_a_confirmed_publish_preserves_the_claim(settings):
    provider = Instagram()
    app = make_app(settings, provider)
    with TestClient(app, raise_server_exceptions=False) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        store = app.state.store
        original = store.update_post

        def refuse(post_id, status, media_id=None, **kwargs):
            if status == "published":
                raise sqlite3.OperationalError("disk I/O error")
            return original(post_id, status, media_id, **kwargs)

        store.update_post = refuse
        try:
            response = publish_ready(client, post["id"])
        finally:
            store.update_post = original

        assert response.status_code == 503, response.text
        assert response.json()["outcome"] == "unknown"
        assert "Instagram accepted the publish" in response.json()["detail"]
        assert "sqlite" not in response.text.lower()
        assert "disk" not in response.text.lower()
        assert provider.publish_calls == 1
        # The job is still claimed: no later request can publish it again.
        scope = store.workspace.set(client.get("/session").json()["workspace"]["id"])
        try:
            assert store.get_post(post["id"])["status"] == "publishing"
        finally:
            store.workspace.reset(scope)
        assert publish_ready(client, post["id"]).status_code == 409
        assert provider.publish_calls == 1


def test_a_pre_dispatch_sqlite_failure_is_not_reported_as_unknown(settings):
    """Only the post-dispatch boundary maps SQLite errors to ``unknown``."""
    provider = Instagram()
    app = make_app(settings, provider)
    with TestClient(app) as client:
        signup(client)
        account = connect(client)
        store = app.state.store
        original = store.create_attempt

        def refuse(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        store.create_attempt = refuse
        try:
            response = create_instagram_post(client, account)
        finally:
            store.create_attempt = original

        assert response.status_code == 503
        # Nothing was dispatched, and the message says exactly that.
        assert response.json()["detail"].startswith("Could not record this post safely.")
        assert "Nothing was sent for this request" in response.json()["detail"]
        assert provider.create_calls == 0


def test_a_history_failure_that_also_cannot_be_recorded_is_a_sanitised_unknown(settings):
    """History loss alone is a 200; history loss *plus* an unwritable receipt is not.

    ``_record_instagram_history`` normally absorbs its own SQLite error and notes
    it on the receipt. If that note also cannot be written, the error escapes
    ``update_post`` and must still be the same sanitised 503/unknown — never a
    bare 500, and never a retryable failure that would publish twice.
    """
    provider = Instagram()
    app = make_app(settings, provider)
    with TestClient(app, raise_server_exceptions=False) as client:
        signup(client)
        account = connect(client)
        post = create_instagram_post(client, account).json()
        store = app.state.store
        save_history = store._save_instagram_history
        update_attempt = store.update_attempt

        def refuse_history(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        def refuse_note(attempt_id, **fields):
            if fields.get("error"):
                raise sqlite3.OperationalError("disk I/O error")
            return update_attempt(attempt_id, **fields)

        store._save_instagram_history = staticmethod(refuse_history)
        store.update_attempt = refuse_note
        try:
            response = publish_ready(client, post["id"])
        finally:
            store._save_instagram_history = staticmethod(save_history)
            store.update_attempt = update_attempt

        assert response.status_code == 503, response.text
        assert response.json()["outcome"] == "unknown"
        assert "Instagram accepted the publish" in response.json()["detail"]
        assert provider.publish_calls == 1
        # The job's status transition committed before the history write, so the
        # post is durably published and can never be published a second time.
        scope = store.workspace.set(client.get("/session").json()["workspace"]["id"])
        try:
            assert store.get_post(post["id"])["status"] == "published"
        finally:
            store.workspace.reset(scope)
        assert publish_ready(client, post["id"]).status_code == 200
        assert provider.publish_calls == 1

