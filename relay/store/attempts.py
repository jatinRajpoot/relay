"""Durable per-destination publishing receipts.

Ownership and source of truth
-----------------------------
``publishing_attempts`` is the recovery ledger: one row per *destination* (an
Instagram job or a YouTube upload). A row is inserted **before** any external
side-effecting call, then claimed, then updated with the provider's answer.

The row is not the whole truth for Instagram: the existing ``instagram_posts``
job remains authoritative for container processing, and the route mirror keeps
the attempt's status in step with it. The ledger exists so a browser reload,
process restart, or lost response can still answer "what did we send, and what
do we know about it?" without ever re-dispatching a publish.

Status vocabulary (consumed by the frontend)
--------------------------------------------
``pending``     receipt written, no external call dispatched yet
``submitted``   a side-effecting call was claimed/dispatched; outcome not stored
``processing``  Instagram container accepted and still processing
``ready``       Instagram container processed; awaiting explicit confirmation
``published``   provider confirmed the destination is live
``failed``      provider definitively rejected it (safe to retry manually)
``expired``     Instagram container expired before it was published
``unknown``     the outcome is uncertain; verify with the provider, never auto-retry

``failed`` and ``unknown`` are retained deliberately: they are what the user
needs after a restart, and neither may be retried automatically.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
import time

STATUSES = frozenset(
    {"pending", "submitted", "processing", "ready", "published", "failed", "expired", "unknown"}
)
# Everything the recovery list shows for "active": work in flight, plus outcomes
# that still need a human decision. Terminal success/expiry is history.
ACTIVE_STATUSES = frozenset(
    {"pending", "submitted", "processing", "ready", "failed", "unknown"}
)
# Only a claimed, dispatched call can become uncertain with time. ``processing``
# and ``ready`` are known provider states and are never downgraded here.
STALE_STATUSES = frozenset({"submitted"})
# A receipt that was never claimed never reached the provider (the claim write
# precedes the call), so it is safe to record as a definite non-start.
UNSTARTED_STATUSES = frozenset({"pending"})

# How long a receipt may stay unclaimed before it is treated as a definite
# non-start. This is a safety net for a crashed process, not a timeout for the
# create->claim gap, which is milliseconds.
PENDING_SECONDS = 300

# How long a claim may stay unconfirmed before it is treated as uncertain.
# Instagram publish calls are short; a YouTube upload may legitimately stream
# for up to the provider's 600 s timeout, so it gets a wider bound.
STALE_SECONDS = {"instagram": 300, "youtube": 900}

# Why a definitively-failed receipt cannot be retried yet. ``none`` means the
# user may retry immediately; ``delay`` means the provider asked us to wait;
# ``reconnect`` means the stored credential must be replaced first.
RETRY_KINDS = frozenset({"none", "delay", "reconnect"})


MAX_LIMIT = 100
DEFAULT_LIMIT = 50
MAX_CURSOR_LENGTH = 512

# The receipt columns every read returns, plus the facts a retry decision needs
# from the Instagram job (its status and the publish key the client used) and
# from the account (whether an Instagram credential is known to have expired).
# Selecting them here keeps ``retry_action`` a pure function of one row; only a
# timestamp is read, never token material.
_ATTEMPT_COLUMNS = """
    p.*, a.name AS account_name, a.token_updated_at AS account_token_updated_at,
    a.expires_at AS account_expires_at, j.status AS post_status
"""

# ``failure_stage`` records *where* a definite failure happened, because a
# ``failed`` Instagram job alone cannot distinguish two opposite situations:
#
# ``container`` the media never became a publishable container (processing
#             ERROR/EXPIRED). Re-sending the publish would be refused forever.
# ``publish`` the container was usable and the publish call itself was refused
#             (a definite non-effect), so the same container may be re-sent.
#
# A ``failed`` row with no stage (written by an older release) is treated as
# *not* reusable: never assume an unknown container is safe to publish again.
FAILURE_STAGES = frozenset({"container", "publish"})

CONTAINER_FAILED_ERROR = (
    "Instagram could not process this media, so the container can't be published. "
    "Edit a copy and try again."
)

_UNSAFE = "The provider call did not complete. Verify before retrying."


def fingerprint(payload: dict) -> str:
    """Bind an idempotency key to the exact semantic request it was issued for."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def safe_error(error) -> str:
    """Store only messages that are already sanitised for end users.

    Provider transport errors can carry URLs or credentials, so anything that is
    not a deliberate, sanitised ``detail`` collapses to a generic sentence.
    """
    detail = getattr(error, "detail", None)
    return detail if isinstance(detail, str) and detail else _UNSAFE


def retry_metadata(error, now=None) -> tuple[str, int | None]:
    """Turn a definite provider refusal into retry strategy metadata.

    Only a call that provably had no effect may be retried, and even then the
    interface must be told *how*: wait for a provider-stated delay, reconnect the
    credential, or retry now. ``retry_at`` is only set from a validated header.
    """
    now = int(time.time()) if now is None else now
    kind = getattr(error, "kind", "rejected")
    if kind == "auth":
        return "reconnect", None
    if kind == "rate_limited":
        return "delay", now + (getattr(error, "retry_after", None) or 60)
    return "none", None


def _encode_cursor(created_at: int, attempt_id: str) -> str:
    raw = f"{created_at}:{attempt_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[int, str]:
    """Parse an opaque ``(created_at, id)`` cursor, rejecting anything malformed."""
    if not isinstance(cursor, str) or not cursor or len(cursor) > MAX_CURSOR_LENGTH:
        raise ValueError("invalid cursor")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        created_at, attempt_id = raw.split(":", 1)
        created_at = int(created_at)
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError):
        raise ValueError("invalid cursor") from None
    if created_at < 0 or not attempt_id or len(attempt_id) > 128:
        raise ValueError("invalid cursor")
    if any(character < " " for character in attempt_id):
        raise ValueError("invalid cursor")
    return created_at, attempt_id


class AttemptStore:
    def create_attempt(
        self,
        *,
        account_id,
        provider,
        status="pending",
        post_id=None,
        provider_ref=None,
        provider_url=None,
        media_name=None,
        media_type=None,
        media_url=None,
        title=None,
        caption="",
        privacy_status=None,
        idempotency_key=None,
        request_fingerprint=None,
        error=None,
        retry_kind=None,
        retry_at=None,
        claimed_at=None,
        created_at=None,
        updated_at=None,
        attempt_id=None,
        workspace_id=None,
    ):
        now = int(time.time())
        attempt_id = attempt_id or secrets.token_hex(16)
        created_at = created_at if created_at is not None else now
        updated_at = updated_at if updated_at is not None else created_at
        with self.connection() as db:
            db.execute(
                """
                INSERT INTO publishing_attempts(
                    id, workspace_id, account_id, provider, status, post_id,
                    provider_ref, provider_url, media_name, media_type, media_url,
                    title, caption, privacy_status, idempotency_key,
                    request_fingerprint, error, retry_kind, retry_at,
                    created_at, updated_at, claimed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    workspace_id if workspace_id is not None else self.workspace.get(),
                    account_id,
                    provider,
                    status,
                    post_id,
                    provider_ref,
                    provider_url,
                    media_name,
                    media_type,
                    media_url,
                    title,
                    caption or "",
                    privacy_status,
                    idempotency_key,
                    request_fingerprint,
                    error,
                    retry_kind,
                    retry_at,
                    created_at,
                    updated_at,
                    claimed_at,
                ),
            )
        return self.get_attempt(attempt_id)

    def get_attempt(self, attempt_id):
        with self.connection() as db:
            row = db.execute(
                f"""
                SELECT {_ATTEMPT_COLUMNS}
                FROM publishing_attempts p JOIN accounts a ON a.id=p.account_id
                LEFT JOIN instagram_posts j ON j.id=p.post_id
                WHERE p.id=? AND a.workspace_id=?
                """,
                (attempt_id, self.workspace.get()),
            ).fetchone()
        return dict(row) if row else None

    def attempt_for_post(self, post_id):
        with self.connection() as db:
            row = db.execute(
                f"""SELECT {_ATTEMPT_COLUMNS}
                   FROM publishing_attempts p JOIN accounts a ON a.id=p.account_id
                   LEFT JOIN instagram_posts j ON j.id=p.post_id
                   WHERE p.post_id=? AND a.workspace_id=?
                   ORDER BY p.created_at LIMIT 1""",
                (post_id, self.workspace.get()),
            ).fetchone()
        return dict(row) if row else None

    def find_attempt_by_key(self, provider, account_id, key, *, publish=False):
        column = "publish_idempotency_key" if publish else "idempotency_key"
        with self.connection() as db:
            row = db.execute(
                f"""SELECT {_ATTEMPT_COLUMNS}
                    FROM publishing_attempts p JOIN accounts a ON a.id=p.account_id
                    LEFT JOIN instagram_posts j ON j.id=p.post_id
                    WHERE p.{column}=? AND p.provider=? AND p.account_id=?
                      AND a.workspace_id=?""",
                (key, provider, account_id, self.workspace.get()),
            ).fetchone()
        return dict(row) if row else None

    def claim_attempt(self, attempt_id, *, expected=("pending",), publish_key=None,
                      publish_fingerprint=None):
        """Durably mark a destination as dispatched before the external call.

        Returns ``False`` when the row was not in an expected state (another
        request already claimed it), in which case the caller must not call the
        provider.
        """
        now = int(time.time())
        placeholders = ", ".join("?" for _ in expected)
        with self.connection() as db:
            updated = db.execute(
                f"""
                UPDATE publishing_attempts
                SET status='submitted', claimed_at=?, updated_at=?,
                    publish_idempotency_key=COALESCE(?, publish_idempotency_key),
                    publish_fingerprint=COALESCE(?, publish_fingerprint)
                WHERE id=? AND status IN ({placeholders})
                """,
                (now, now, publish_key, publish_fingerprint, attempt_id, *expected),
            ).rowcount
        return updated == 1

    def update_attempt(self, attempt_id, **fields):
        allowed = {
            "status", "post_id", "provider_ref", "provider_url", "media_name",
            "media_type", "media_url", "title", "caption", "privacy_status",
            "idempotency_key", "request_fingerprint", "error", "claimed_at",
            "retry_kind", "retry_at", "failure_stage",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return self.get_attempt(attempt_id)
        updates["updated_at"] = int(time.time())
        assignments = ", ".join(f"{column}=?" for column in updates)
        with self.connection() as db:
            db.execute(
                f"UPDATE publishing_attempts SET {assignments} WHERE id=?",
                (*updates.values(), attempt_id),
            )
        return self.get_attempt(attempt_id)

    def release_claim(self, attempt_id, *, status, error=None, retry_kind=None, retry_at=None):
        """Return a claimed receipt to a retryable state after a definite non-effect.

        ``unknown`` is accepted alongside ``submitted`` because the stale sweep may
        have aged this same claim while the provider call was still in flight; the
        provider's definitive answer is authoritative over that timeout. The claim
        timestamp is cleared so the next attempt takes a fresh one and a later
        sweep cannot mistake the released row for in-flight work.
        """
        with self.connection() as db:
            updated = db.execute(
                """
                UPDATE publishing_attempts
                SET status=?, error=?, retry_kind=?, retry_at=?, claimed_at=NULL, updated_at=?
                WHERE id=? AND status IN ('submitted', 'unknown')
                """,
                (status, error, retry_kind, retry_at, int(time.time()), attempt_id),
            ).rowcount
        return updated == 1

    def reopen_failed_attempt(self, attempt_id):
        """Re-arm a definitively-failed receipt for one more dispatch.

        Only ``failed`` may be reopened: ``unknown`` may have reached the provider
        and must never be re-sent. The row keeps its id, key, and fingerprint, so a
        retry of the same logical submission stays one ledger entry.

        The ``WHERE status='failed'`` predicate is the compare-and-swap: of two
        simultaneous retries of the same key, exactly one moves the row to
        ``pending``. The loser gets ``False`` and answers 409 from the ledger
        instead of dispatching a second container or upload.
        """
        with self.connection() as db:
            updated = db.execute(
                """
                UPDATE publishing_attempts
                SET status='pending', claimed_at=NULL, error=NULL,
                    retry_kind=NULL, retry_at=NULL, failure_stage=NULL, updated_at=?
                WHERE id=? AND status='failed'
                """,
                (int(time.time()), attempt_id),
            ).rowcount
        return updated == 1

    @staticmethod
    def retry_is_open(row, now=None):
        """Whether a failed receipt may be dispatched again right now.

        A ``reconnect`` refusal is cleared when the account is reconnected (see
        ``save_account``), so this only has to enforce the provider's cooldown.
        """
        if row["status"] != "failed":
            return False
        now = int(time.time()) if now is None else now
        if row["retry_kind"] == "reconnect":
            return False
        delayed = row["retry_kind"] == "delay" and row["retry_at"] and row["retry_at"] > now
        return not delayed

    @staticmethod
    def reconnect_required(row, now=None):
        """Whether replacing the credential is the only remedy for this receipt.

        Two independent conditions mean the same thing to the user: the provider
        said the credential is unusable, or the stored Instagram credential is
        known to have expired. Both are checked in one place so the projection and
        the endpoint's retry gate can never disagree about which one applies.
        """
        if row["retry_kind"] == "reconnect":
            return True
        now = int(time.time()) if now is None else now
        return _instagram_credential_expired(row, now)

    def attempt_claim_is_stale(self, post_id, timeout=None):
        """Whether an Instagram publish claim has outlived its bound. No provider call."""
        timeout = STALE_SECONDS["instagram"] if timeout is None else timeout
        attempt = self.attempt_for_post(post_id)
        if not attempt or attempt["status"] != "submitted" or not attempt["claimed_at"]:
            return False
        return attempt["claimed_at"] < int(time.time()) - timeout

    def reconcile_stale_attempts(self, now=None):
        """Conservatively age out unconfirmed claims for this workspace.

        This is a pure database transition: it never dispatches, replays, or
        queries a provider, so calling it from a list/read can never re-send a
        publish. A claim that was never taken is a definite non-start; a claim
        that was taken but never answered is uncertain and keeps its provider
        reference.
        """
        now = int(time.time()) if now is None else now
        workspace = self.workspace.get()
        with self.connection() as db:
            for provider, timeout in STALE_SECONDS.items():
                cutoff = now - timeout
                placeholders = ", ".join("?" for _ in STALE_STATUSES)
                # No ``post_id`` filter: a YouTube upload has no Instagram job, so
                # requiring one would leave its claim "submitted" forever.
                stale = [
                    row[0]
                    for row in db.execute(
                        f"""
                        SELECT post_id FROM publishing_attempts
                        WHERE workspace_id=? AND provider=? AND status IN ({placeholders})
                          AND (claimed_at IS NULL OR claimed_at < ?)
                        """,
                        (workspace, provider, *STALE_STATUSES, cutoff),
                    )
                ]
                db.execute(
                    f"""
                    UPDATE publishing_attempts
                    SET status='unknown', updated_at=?,
                        error=COALESCE(error, 'The provider did not confirm this attempt in time. '
                                              || 'Verify it before retrying.'),
                        retry_kind=NULL, retry_at=NULL
                    WHERE workspace_id=? AND provider=? AND status IN ({placeholders})
                      AND (claimed_at IS NULL OR claimed_at < ?)
                    """,
                    (now, workspace, provider, *STALE_STATUSES, cutoff),
                )
                # The Instagram job is authoritative for its own status, so an aged
                # claim must be reflected there too. This is a database-only
                # transition: no provider call is made, so nothing is re-sent.
                for post_id in stale:
                    if post_id is None:
                        continue
                    db.execute(
                        "UPDATE instagram_posts SET status='unknown' "
                        "WHERE id=? AND status='publishing'",
                        (post_id,),
                    )
                db.execute(
                    """
                    UPDATE publishing_attempts
                    SET status='failed', updated_at=?,
                        error=COALESCE(error, 'This attempt never started. It is safe to try again.'),
                        retry_kind=COALESCE(retry_kind, 'none'), retry_at=NULL
                    WHERE workspace_id=? AND provider=? AND status='pending' AND created_at < ?
                    """,
                    (now, workspace, provider, now - PENDING_SECONDS),
                )

    def list_attempts(self, *, status="active", limit=DEFAULT_LIMIT, cursor=None):
        """Workspace-scoped, stable descending page of receipts.

        Ordering is ``(created_at DESC, id DESC)`` and the cursor carries both
        halves, so equal timestamps neither duplicate nor skip a row.
        """
        limit = max(1, min(int(limit), MAX_LIMIT))
        params = [self.workspace.get()]
        where = ["a.workspace_id=?"]
        if status == "active":
            placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
            where.append(f"p.status IN ({placeholders})")
            params.extend(sorted(ACTIVE_STATUSES))
        if cursor:
            created_at, attempt_id = decode_cursor(cursor)
            where.append("(p.created_at < ? OR (p.created_at = ? AND p.id < ?))")
            params.extend((created_at, created_at, attempt_id))
        params.append(limit + 1)
        with self.connection() as db:
            rows = [
                dict(row)
                for row in db.execute(
                    f"""
                    SELECT {_ATTEMPT_COLUMNS}
                    FROM publishing_attempts p JOIN accounts a ON a.id=p.account_id
                    LEFT JOIN instagram_posts j ON j.id=p.post_id
                    WHERE {" AND ".join(where)}
                    ORDER BY p.created_at DESC, p.id DESC
                    LIMIT ?
                    """,
                    params,
                )
            ]
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = _encode_cursor(last["created_at"], last["id"])
        return rows, next_cursor

    @staticmethod
    def public_attempt(row):
        """Project a receipt for the API. Never includes tokens or provider payloads.

        ``request_key`` is the client's own CREATE/UPLOAD ``Idempotency-Key`` for
        this destination, or ``None`` for an unkeyed/legacy attempt. It exists so a
        client that lost the response can correlate *exactly* one receipt to the
        intent it sent — provider + account + key — instead of guessing from
        timestamps, which can attach an unrelated newer or simultaneous post.

        ``publish_key`` is the client's own confirmation key, once a publish was
        claimed, or ``None`` before that. It is exposed for the same reason: a
        client that lost its local draft can still retry a definitively failed
        confirmation with the *exact* key it originally used, keeping one ledger
        row for one logical submission. Without it the client would have to
        invent a fresh key (weakening duplicate suppression) or give up.

        Neither key is a secret or a capability: both are random values the
        client generated, they grant nothing, and every read is still
        authenticated and workspace-scoped (the row can only be reached through
        a join on an account in the caller's workspace). The request fingerprint
        is deliberately not exposed: it is a hash of the payload and exists only
        for server-side conflict detection.
        """
        status = row["status"]
        retry_kind = row["retry_kind"] if row["retry_kind"] in RETRY_KINDS else None
        retry_at = row["retry_at"]
        failure_stage = row.get("failure_stage")
        return {
            "id": row["id"],
            "provider": row["provider"],
            "account_id": row["account_id"],
            "account_name": row.get("account_name"),
            "request_key": row.get("idempotency_key"),
            "publish_key": row.get("publish_idempotency_key"),
            "status": status,
            "next_action": _next_action(row),
            "retry_action": _retry_action(row),
            "retry_kind": retry_kind,
            "retry_at": retry_at,
            "failure_stage": failure_stage if failure_stage in FAILURE_STAGES else None,
            "post_id": row["post_id"],
            "provider_ref": row["provider_ref"],
            "provider_url": row["provider_url"],
            "verification_url": _verification_url(row),
            "media_name": row["media_name"],
            "media_type": row["media_type"],
            "media_url": row["media_url"],
            "title": row["title"],
            "caption": row["caption"] or "",
            "privacy_status": row["privacy_status"],
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "claimed_at": row["claimed_at"],
        }


def _next_action(row):
    """The single control the interface should offer for this receipt.

    ``failed`` is the only retryable state, and even then the retry may be gated:
    ``retry`` means a new attempt is safe now, ``retry_later`` means the provider
    asked for a delay (``retry_at``), ``reconnect`` means the credential must be
    replaced first, ``edit_copy`` means this destination can never be re-sent.
    It is never ``retry`` for a state that a resend would refuse — the decision
    is derived from ``_retry_action`` so the two can never disagree.
    """
    status = row["status"]
    if status != "failed":
        return _NEXT_ACTION.get(status, "none")
    action = _retry_action(row)
    return {
        "resend": "retry",
        "wait": "retry_later",
        "reconnect": "reconnect",
        "new_attempt": "edit_copy",
    }.get(action["kind"] if action else None, "edit_copy")


def _instagram_credential_expired(row, now):
    """Whether the stored Instagram credential is known to have expired.

    Instagram tokens are long-lived and are *not* refreshed automatically by the
    publish path (unlike YouTube's, which ``valid_account`` refreshes), so an
    expired one is a hard stop that only reconnecting clears. Only the timestamp
    is read here — never the ciphertext — so a list projection cannot leak or
    decrypt credentials. YouTube is deliberately excluded: its access token is
    refreshed automatically, so an expired timestamp is not a reconnect signal.
    """
    expires_at = row.get("account_expires_at")
    return (
        row["provider"] == "instagram"
        and isinstance(expires_at, int)
        and expires_at <= now
    )


def _retry_action(row):
    """Exactly how a definitively failed destination may be retried, or ``None``.

    ``next_action == "retry"`` says a retry is safe; this says *which request*
    implements it, so the interface never offers a "Try again" that the endpoint
    would answer with 409. The three failures differ and are not interchangeable:

    * Instagram container creation failed (no job exists): repeat
      ``POST /instagram/posts`` with the same request key. There is no job to
      publish, so the publish endpoint would 404/409.
    * Instagram publish failed (a job exists and its container is still usable):
      repeat ``POST /instagram/posts/{post_id}/publish`` with the same publish
      key. Repeating the create would make a second container.
    * YouTube upload failed: repeat ``POST /youtube/videos`` with the same
      request key.

    ``key`` names which key the client must reuse — ``request`` is the value in
    ``request_key``, ``publish`` is the value in ``publish_key``, ``none`` means
    the original request was unkeyed (the retry is accepted, but there is no
    duplicate suppression to offer). A retry is *only* ever offered for a state
    that the endpoint accepts; anything else is ``new_attempt`` (start a new
    post) or a gate (``wait``/``reconnect``).

    A ``failed`` Instagram *job* is not by itself proof that its container can
    be published again: the job is also ``failed`` when the media never became a
    usable container (processing ``ERROR``). Only an explicit
    ``failure_stage == "publish"`` — a refusal of the publish call itself — makes
    the same container resendable. A row written before that provenance existed
    is treated as unsafe, because guessing "reusable" is what re-arms a dead
    container forever.
    """
    if row["status"] != "failed":
        return None
    now = int(time.time())
    if AttemptStore.reconnect_required(row, now):
        # Either the provider said the credential is unusable, or the stored
        # Instagram credential is known to have expired. No resend can succeed
        # until it is replaced, even if the receipt's timestamp is old.
        return {"kind": "reconnect", "endpoint": None, "key": "none", "retry_at": None}
    if not AttemptStore.retry_is_open(row, now):
        return {"kind": "wait", "endpoint": None, "key": "none", "retry_at": row["retry_at"]}
    if row["provider"] == "instagram":
        if row["post_id"]:
            reusable = row.get("post_status") == "ready" or (
                row.get("post_status") == "failed"
                and row.get("failure_stage") == "publish"
            )
            if reusable:
                return {
                    "kind": "resend",
                    "endpoint": f"/instagram/posts/{row['post_id']}/publish",
                    "key": "publish" if row.get("publish_idempotency_key") else "none",
                    "retry_at": None,
                }
            # A dead/unknown container (or one whose provenance we cannot prove)
            # must never be re-published. Edit-a-copy is the only honest option.
            return {"kind": "new_attempt", "endpoint": None, "key": "none", "retry_at": None}
        return {
            "kind": "resend",
            "endpoint": "/instagram/posts",
            "key": "request" if row.get("idempotency_key") else "none",
            "retry_at": None,
        }
    return {
        "kind": "resend",
        "endpoint": "/youtube/videos",
        "key": "request" if row.get("idempotency_key") else "none",
        "retry_at": None,
    }


_NEXT_ACTION = {
    "pending": "wait",
    "submitted": "check",
    "processing": "check",
    "ready": "publish",
    "published": "none",
    "failed": "retry",
    "expired": "edit_copy",
    "unknown": "verify",
}


def _verification_url(row):
    """A safe place for the user to confirm an uncertain outcome themselves."""
    if row["status"] not in {"unknown", "submitted"}:
        return None
    if row["provider"] == "youtube":
        return "https://studio.youtube.com"
    return "https://www.instagram.com"
