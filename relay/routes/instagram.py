from __future__ import annotations

import sqlite3
import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from relay.providers import ProviderError
from relay.schemas import InstagramPost, PublishRequest, normalize_idempotency_key
from relay.store import StorageError
from relay.store.attempts import (
    CONTAINER_FAILED_ERROR,
    fingerprint,
    retry_metadata,
    safe_error,
)

from .context import CredentialsUnreadable, RouteContext

IDEMPOTENCY_HEADER = "Idempotency-Key"
_KEY_REUSED = "This request key was already used for a different post. Start a new post."


def _resolve_key(header_value, body_value):
    """Merge the header and body key, refusing an ambiguous pair.

    The body field is validated by the request model, but the header is not, so a
    malformed header must be a 422 here rather than an unhandled ``ValueError``
    (a 500 that also leaks a stack trace in debug mode).
    """
    try:
        header_key = normalize_idempotency_key(header_value)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    body_key = normalize_idempotency_key(body_value)
    if header_key and body_key and header_key != body_key:
        raise HTTPException(422, "Send the idempotency key once, as a header or in the body.")
    return header_key or body_key


def register_instagram_routes(app, context: RouteContext):
    settings = context.settings
    store = context.store
    protected = context.protected
    valid_account = context.valid_account
    account_token = context.account_token
    sync_post = context.sync_post
    post_or_404 = context.post_or_404

    def duplicate_response(attempt, post):
        """Answer a repeated key from the durable receipt. Never dispatches."""
        if post is not None:
            return post
        return JSONResponse(
            status_code=202,
            content={
                "attempt": store.public_attempt(attempt),
                "detail": "A previous attempt with this key is still being resolved.",
            },
        )

    def retry_gate(attempt):
        """Refuse a retry the provider told us to delay, with a precise reason."""
        if attempt["retry_kind"] == "reconnect":
            raise HTTPException(409, "Reconnect this account before trying again.")
        remaining = max(1, (attempt["retry_at"] or 0) - int(time.time()))
        raise HTTPException(
            429,
            "The provider asked us to wait before retrying. Try again shortly.",
            headers={"Retry-After": str(remaining)},
        )

    def require_retry_open(attempt):
        """The one gate every explicit retry passes, keyed or not.

        Cooldown and reconnect protection must not depend on which branch a
        request happens to take: a keyed retry, a keyless retry, and a retry of a
        job whose receipt records a refusal all pass through here. It uses the
        same predicate as the projection, so the recipe the client was shown and
        the answer it gets can never disagree — including when an Instagram
        credential expired *after* a cooldown was recorded.
        """
        if store.reconnect_required(attempt):
            raise HTTPException(409, "Reconnect this account before trying again.")
        if not store.retry_is_open(attempt):
            retry_gate(attempt)

    def repeated_key(attempt, post):
        """Resolve a repeated key without ever dispatching implicitly.

        ``None`` means the key names a definitively failed attempt that may be
        retried now; the caller re-arms that same receipt. A failed attempt that
        already owns a job is *not* re-armed: its container exists, so re-sending
        the create would make a second one. Every other state answers from the
        ledger.
        """
        if attempt["status"] == "failed" and attempt["post_id"] is None:
            require_retry_open(attempt)
            return None
        return duplicate_response(attempt, post)

    @app.post("/instagram/posts", dependencies=protected, tags=["Instagram"], status_code=201)
    async def create_post(body: InstagramPost, request: Request):
        """Create an unpublished image/Reel container. Check status, then call /publish."""
        if settings.local_media_only:
            raise HTTPException(
                409,
                "Local preview mode stores files on this computer. Instagram publishing is disabled.",
            )
        account = await valid_account(body.account_id, "instagram")
        key = _resolve_key(request.headers.get(IDEMPOTENCY_HEADER), body.idempotency_key)
        request_fingerprint = fingerprint(
            {
                "operation": "instagram.create",
                "account_id": account["id"],
                "media_type": body.media_type,
                "media_url": str(body.media_url),
                "caption": body.caption,
            }
        )
        if key:
            existing = store.find_attempt_by_key("instagram", account["id"], key)
            if existing:
                if existing["request_fingerprint"] != request_fingerprint:
                    raise HTTPException(409, _KEY_REUSED)
                answer = repeated_key(
                    existing,
                    store.get_post(existing["post_id"]) if existing["post_id"] else None,
                )
                if answer is not None:
                    return answer
                # A definite refusal: re-arm the same receipt for one more dispatch.
                if not store.reopen_failed_attempt(existing["id"]):
                    raise HTTPException(409, "This request is already being processed.")
                attempt = store.get_attempt(existing["id"])
            else:
                attempt = None
        else:
            attempt = None

        if attempt is None:
            # The receipt is durable *before* the side-effecting call. If it cannot
            # be written, nothing is dispatched and the caller gets a safe error.
            try:
                attempt = store.create_attempt(
                    account_id=account["id"],
                    provider="instagram",
                    status="pending",
                    media_type=body.media_type,
                    media_url=str(body.media_url),
                    caption=body.caption,
                    idempotency_key=key,
                    request_fingerprint=request_fingerprint,
                )
            except sqlite3.IntegrityError:
                # A concurrent request with the same key won the insert.
                existing = (
                    store.find_attempt_by_key("instagram", account["id"], key) if key else None
                )
                if existing and existing["request_fingerprint"] == request_fingerprint:
                    answer = repeated_key(
                        existing,
                        store.get_post(existing["post_id"]) if existing["post_id"] else None,
                    )
                    if answer is not None:
                        return answer
                    if not store.reopen_failed_attempt(existing["id"]):
                        raise HTTPException(409, "This request is already being processed.") from None
                    attempt = store.get_attempt(existing["id"])
                else:
                    raise HTTPException(409, "This request is already being processed.") from None
            except sqlite3.Error:
                # No durable receipt means no dispatch: this request sent nothing. A
                # previous keyed attempt may still exist, so the message does not
                # promise that the destination was never touched.
                raise HTTPException(
                    503,
                    "Could not record this post safely. Nothing was sent for this request. "
                    "Try again.",
                ) from None

        try:
            claimed = store.claim_attempt(attempt["id"], expected=("pending",))
        except sqlite3.Error:
            # The claim could not be persisted, so nothing was dispatched for THIS
            # request. A previous keyed attempt may still exist, so the wording does
            # not promise the destination was never touched.
            raise HTTPException(
                503,
                "Could not record this post safely. Nothing was sent for this request. "
                "Try again.",
            ) from None
        if not claimed:
            raise HTTPException(409, "This request is already being processed.")
        try:
            container_id = await app.state.providers.create_instagram_post(
                account["external_id"],
                account_token(account),
                body.media_type,
                str(body.media_url),
                body.caption,
            )
        except ProviderError as error:
            if error.kind in {"rate_limited", "auth"}:
                # Definite non-effect: the container was not created. Keep the
                # receipt retryable and tell the client how to retry.
                kind, retry_at = retry_metadata(error)
                store.release_claim(
                    attempt["id"],
                    status="failed",
                    error=safe_error(error),
                    retry_kind=kind,
                    retry_at=retry_at,
                )
            else:
                store.update_attempt(
                    attempt["id"],
                    status="unknown" if error.ambiguous else "failed",
                    error=safe_error(error),
                    retry_kind=None if error.ambiguous else "none",
                )
            raise
        except HTTPException as error:
            # The provider was never reached. An unreadable credential is fixed
            # only by reconnecting, so the receipt must say so; a resend with the
            # same credential would fail identically forever.
            store.update_attempt(
                attempt["id"],
                status="failed",
                error=safe_error(error),
                retry_kind="reconnect" if isinstance(error, CredentialsUnreadable) else "none",
            )
            raise
        except Exception:  # any local failure after dispatch is uncertain.
            # An unexpected local failure after dispatch: the container may exist.
            store.update_attempt(
                attempt["id"],
                status="unknown",
                error="The container request did not complete. Verify before retrying.",
            )
            raise

        try:
            post = store.create_post(
                account["id"],
                container_id,
                body.media_type,
                str(body.media_url),
                body.caption,
                attempt_id=attempt["id"],
            )
        except Exception:  # noqa: BLE001 - the container may exist; record uncertainty.
            # The container exists but cannot be tracked. Never lose that fact.
            store.update_attempt(
                attempt["id"],
                status="unknown",
                provider_ref=container_id,
                error="The container was created but could not be saved. Verify before retrying.",
            )
            raise HTTPException(
                502, "The container was created but could not be saved. Verify before retrying."
            ) from None
        store.update_attempt(
            attempt["id"],
            status="processing",
            post_id=post["id"],
            provider_ref=container_id,
            claimed_at=None,
        )
        return post

    @app.get("/instagram/posts/{post_id}", dependencies=protected, tags=["Instagram"])
    async def post_status(post_id: str):
        """Poll at most once per minute until ready. Processing can take several minutes."""
        return await sync_post(post_or_404(post_id))

    @app.post("/instagram/posts/{post_id}/publish", dependencies=protected, tags=["Instagram"])
    async def publish(post_id: str, request: Request, body: PublishRequest | None = None):
        """Publish a ready container immediately. Repeated calls return the saved published result."""
        if settings.local_media_only:
            raise HTTPException(
                409,
                "Local preview mode stores files on this computer. Instagram publishing is disabled.",
            )
        post = await sync_post(post_or_404(post_id))
        if post["status"] == "published":
            return post
        key = _resolve_key(
            request.headers.get(IDEMPOTENCY_HEADER), body.idempotency_key if body else None
        )
        publish_fingerprint = fingerprint(
            {
                "operation": "instagram.publish",
                "account_id": post["account_id"],
                "post_id": post["id"],
                "container_id": post["container_id"],
            }
        )
        if key:
            existing = store.find_attempt_by_key(
                "instagram", post["account_id"], key, publish=True
            )
            if existing:
                if existing["publish_fingerprint"] != publish_fingerprint:
                    raise HTTPException(409, _KEY_REUSED)
                if existing["status"] != "failed":
                    return duplicate_response(existing, store.get_post(post["id"]))
                # A definitively refused publish with this key: fall through to the
                # explicit retry path below, which re-arms the same receipt.
                require_retry_open(existing)
        if post["status"] not in {"ready", "failed"}:
            raise HTTPException(
                409, f"Post is {post['status']}; only ready posts can be published."
            )
        account = await valid_account(post["account_id"], "instagram")
        # Resolve the token before claiming the post: an unreadable token is a local
        # problem, so the job must stay "ready" (retryable) instead of becoming "unknown".
        try:
            token = account_token(account)
        except CredentialsUnreadable:
            # A receipt written by an earlier release may still advertise a resend
            # (its retry_kind was recorded as "none"). This is the one path where
            # that cannot self-heal on retry, because the token is read before the
            # receipt is touched. Repair it here so the next read tells the truth:
            # the remedy is reconnecting, not resending.
            prior = store.attempt_for_post(post_id)
            if prior and prior["status"] == "failed" and prior["retry_kind"] != "reconnect":
                store.update_attempt(prior["id"], retry_kind="reconnect")
            raise
        attempt = store.attempt_for_post(post_id)
        if post["status"] == "failed":
            # A definitively refused publish provably had no effect, so an explicit
            # retry of the same container is safe. It is only offered when the
            # receipt proves *that* is what failed: a job is also ``failed`` when
            # the media never became a usable container, and re-arming that would
            # dispatch a publish the provider rejects forever.
            if not attempt or attempt["status"] != "failed":
                raise HTTPException(409, "Post is failed; start a new post.")
            if attempt["failure_stage"] != "publish":
                # A dead container, or a legacy row whose provenance is unknown.
                # Neither may be re-armed: only an explicit new attempt is honest.
                raise HTTPException(
                    409,
                    CONTAINER_FAILED_ERROR
                    if attempt["failure_stage"] == "container"
                    else "Relay can't confirm this media is still publishable. "
                    "Edit a copy and try again.",
                )
            require_retry_open(attempt)
            # Re-arm the job and its receipt together, keeping the same ledger row
            # and the same idempotency key for this logical submission.
            if not store.reopen_failed_publish(post_id):
                raise HTTPException(409, "This post is already being published. Check its status.")
        elif attempt and attempt["status"] == "failed":
            # A ready job whose receipt records a refusal (for example after a rate
            # limit released the job). The cooldown still applies even though the
            # job looks publishable, so a keyless retry cannot bypass it.
            require_retry_open(attempt)
            if not store.reopen_failed_attempt(attempt["id"]):
                raise HTTPException(409, "This post is already being published. Check its status.")
        try:
            claimed = store.claim_post(post_id, key, publish_fingerprint)
        except sqlite3.IntegrityError:
            # The same key is already bound to another publish in this account.
            raise HTTPException(409, _KEY_REUSED) from None
        except sqlite3.Error:
            # The claim could not be persisted, so this request dispatched nothing.
            raise HTTPException(
                503,
                "Could not record this publish safely. Nothing was sent for this request. "
                "Try again.",
            ) from None
        if not claimed:
            raise HTTPException(409, "Post is already being published. Check its status.")
        try:
            media_id = await app.state.providers.publish_instagram(
                account["external_id"],
                token,
                post["container_id"],
            )
        except ProviderError as error:
            if error.kind in {"rate_limited", "auth"}:
                # The provider definitively did not publish. Put the job back to
                # "ready" so the user can retry once the limit clears or the
                # credential is reconnected; nothing is re-sent automatically.
                kind, retry_at = retry_metadata(error)
                store.release_publish(
                    post_id,
                    error=safe_error(error),
                    retry_kind=kind,
                    retry_at=retry_at,
                )
                raise
            # A rejection the provider answered is final. Only an ambiguous outcome
            # (transport failure, unreadable reply) may have published, so only that
            # blocks an automatic retry. The container itself was usable — the
            # publish call is what was refused — so record that provenance: it is
            # what makes an explicit resend of the same container safe.
            rejected = not error.ambiguous
            store.update_post(
                post_id,
                "unknown" if error.ambiguous else "failed",
                failure_stage="publish" if rejected else None,
            )
            attempt = store.attempt_for_post(post_id)
            if attempt:
                store.update_attempt(
                    attempt["id"],
                    error=safe_error(error),
                    retry_kind=None if error.ambiguous else "none",
                )
            raise
        except Exception:
            store.update_post(post_id, "unknown")
            raise
        # The provider has published. Recording it must never be reported as a
        # failure the user could "fix" by publishing again, and a storage failure
        # here must never be reported as success either: the job stays claimed
        # (``publishing``), which no later request may re-publish, and the client
        # is told the truth — it may be live, so verify before doing anything.
        #
        # This is the post-dispatch persistence boundary. Both the durable snapshot
        # failing (``StorageError``) and the local SQLite file failing
        # (``sqlite3.Error``: disk full, locked, I/O error) leave the same truth:
        # Instagram published, Relay could not record it. The claim is already
        # durable, so nothing can re-publish; only the report changes. A
        # programming error is deliberately *not* caught here.
        try:
            return store.update_post(post_id, "published", media_id)
        except (StorageError, sqlite3.Error) as error:
            raise StorageError(
                "Instagram accepted the publish, but Relay could not record it. "
                "Check the post on Instagram before doing anything else.",
                outcome="unknown",
            ) from error
