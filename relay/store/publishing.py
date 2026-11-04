import secrets
import sqlite3
import time

# The Instagram job is authoritative for container processing; the recovery receipt
# is kept in step with it so a recovery list never disagrees with the job.
_ATTEMPT_STATUS = {
    "processing": "processing",
    "ready": "ready",
    "publishing": "submitted",
    "published": "published",
    "failed": "failed",
    "expired": "expired",
    "unknown": "unknown",
}


class PublishingStore:
    def create_post(
        self, account_id, container_id, media_type=None, media_url=None, caption="", attempt_id=None
    ):
        post_id = secrets.token_hex(16)
        now = int(time.time())
        with self.connection() as db:
            db.execute(
                """
                INSERT INTO instagram_posts(
                    id, account_id, container_id, created_at, media_type, media_url, caption
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (post_id, account_id, container_id, now, media_type, media_url, caption),
            )
            # Every job must have a receipt. The publishing route writes one before
            # the provider call and passes its id; a job created directly (tests,
            # legacy callers) gets a deterministic one instead, so it is never
            # invisible to recovery.
            linked = 0
            if attempt_id:
                linked = db.execute(
                    """
                    UPDATE publishing_attempts SET post_id=?, updated_at=?
                    WHERE id=? AND post_id IS NULL
                    """,
                    (post_id, now, attempt_id),
                ).rowcount
                if not linked:
                    # The caller proved a receipt existed and was unlinked. Silently
                    # creating a second one would leave two ledgers for one job, so
                    # fail the transaction: the route records the container as
                    # uncertain instead of losing track of it.
                    raise RuntimeError("The publishing receipt could not be linked.")
            else:
                # A job created directly (tests, legacy callers) still gets a
                # deterministic receipt, so it is never invisible to recovery.
                db.execute(
                    """
                    INSERT OR IGNORE INTO publishing_attempts(
                        id, workspace_id, account_id, provider, status, post_id,
                        provider_ref, media_type, media_url, caption, created_at, updated_at
                    ) VALUES (?, ?, ?, 'instagram', 'processing', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"ig-{post_id}",
                        self.workspace.get(),
                        account_id,
                        post_id,
                        container_id,
                        media_type,
                        media_url,
                        caption or "",
                        now,
                        now,
                    ),
                )
        return self.get_post(post_id)

    def get_post(self, post_id):
        with self.connection() as db:
            row = db.execute(
                "SELECT p.* FROM instagram_posts p JOIN accounts a ON a.id=p.account_id "
                "WHERE p.id=? AND a.workspace_id=?", (post_id, self.workspace.get()),
            ).fetchone()
        return dict(row) if row else None

    def _mirror_attempt(self, db, post_id, status, media_id=None, *, failure_stage=None, error=None):
        attempt_status = _ATTEMPT_STATUS.get(status)
        if attempt_status is None:
            return
        # A container that is merely ready again must not erase a recorded provider
        # refusal: the receipt carries the retry strategy (wait/reconnect) that the
        # user still needs. Only a real outcome (published/failed/unknown) may
        # replace it.
        keep_refusal = (
            "AND status NOT IN ('failed')" if attempt_status in {"ready", "processing"} else ""
        )
        # Provenance belongs to the *current* outcome. A row that later reaches a
        # non-failed state must not keep advertising where an old failure happened,
        # and a newer known stage (for example a container that dies after an
        # earlier publish refusal) must replace the older one rather than be
        # coalesced away — otherwise a dead container would look reusable again.
        if attempt_status != "failed":
            failure_stage = None
        # A dead container can never be re-sent, so a cooldown recorded for an
        # earlier refusal is meaningless and would only advertise a wait that ends
        # in "edit a copy" anyway. Clear it with the stage.
        container_failed = failure_stage == "container"
        db.execute(
            f"""
            UPDATE publishing_attempts
            SET status=?, updated_at=?,
                provider_ref=COALESCE(?, provider_ref),
                claimed_at=CASE WHEN ?='submitted' THEN COALESCE(claimed_at, ?) ELSE claimed_at END,
                failure_stage=CASE WHEN ?='failed' THEN ? ELSE NULL END,
                error=CASE WHEN ?='failed' THEN COALESCE(?, error) ELSE error END,
                retry_kind=CASE WHEN ? THEN 'none' ELSE retry_kind END,
                retry_at=CASE WHEN ? THEN NULL ELSE retry_at END
            WHERE post_id=? {keep_refusal}
            """,
            (
                attempt_status,
                int(time.time()),
                media_id,
                attempt_status,
                int(time.time()),
                attempt_status,
                failure_stage,
                attempt_status,
                error,
                container_failed,
                container_failed,
                post_id,
            ),
        )

    def update_post(self, post_id, status, media_id=None, *, failure_stage=None, error=None):
        with self.connection() as db:
            db.execute(
                """
                UPDATE instagram_posts SET status=?, media_id=COALESCE(?, media_id) WHERE id=?
            """,
                (status, media_id, post_id),
            )
            self._mirror_attempt(
                db, post_id, status, media_id, failure_stage=failure_stage, error=error
            )
        if status == "published":
            self._record_instagram_history(post_id)
        return self.get_post(post_id)

    def sync_post_status(self, post_id, previous_status, status, *, failure_stage=None, error=None):
        with self.connection() as db:
            updated = db.execute(
                "UPDATE instagram_posts SET status=? WHERE id=? AND status=?",
                (status, post_id, previous_status),
            ).rowcount
            if updated:
                self._mirror_attempt(
                    db, post_id, status, failure_stage=failure_stage, error=error
                )
        if updated and status == "published":
            self._record_instagram_history(post_id)
        return self.get_post(post_id)

    def _record_instagram_history(self, post_id):
        """Record a confirmed publish without letting history loss undo it.

        The provider has already published, so the status transition is committed
        first. A failure to write the history row is reported to the caller (the
        receipt keeps an explanatory error) but must never roll the job back to a
        retryable state: that would invite a second publish of the same media.
        """
        try:
            with self.connection() as db:
                post = db.execute(
                    "SELECT * FROM instagram_posts WHERE id=?", (post_id,)
                ).fetchone()
                if post:
                    self._save_instagram_history(db, post)
        except sqlite3.Error:
            attempt = self.attempt_for_post(post_id)
            if attempt:
                self.update_attempt(
                    attempt["id"],
                    error="Published, but its history entry could not be saved.",
                )
            return False
        return True

    @staticmethod
    def _save_instagram_history(db, post):
        db.execute(
            """
            INSERT INTO post_history(
                id, provider, account_id, platform_post_id, caption, media_type,
                media_url, published_at
            ) VALUES (?, 'instagram', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, account_id, platform_post_id) DO NOTHING
            """,
            (
                f"instagram-{post['id']}",
                post["account_id"],
                post["media_id"] or post["container_id"],
                post["caption"] or "",
                post["media_type"],
                post["media_url"],
                int(time.time()),
            ),
        )

    def save_youtube_history(
        self,
        account_id,
        video_id,
        title,
        description,
        media_name,
        media_type,
        media_url,
        post_url,
        visibility,
    ):
        with self.connection() as db:
            db.execute(
                """
                INSERT INTO post_history(
                    id, provider, account_id, platform_post_id, title, caption,
                    media_name, media_type, media_url, post_url, visibility, published_at
                ) VALUES (?, 'youtube', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, account_id, platform_post_id) DO NOTHING
                """,
                (
                    # Include the account so two channels recording the same video id
                    # cannot collide on the primary key.
                    f"youtube-{account_id}-{video_id}",
                    account_id,
                    video_id,
                    title,
                    description,
                    media_name,
                    media_type,
                    media_url,
                    post_url,
                    visibility,
                    int(time.time()),
                ),
            )

    def list_post_history(self, limit=200):
        with self.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    """
                    SELECT history.*, accounts.name AS account_name
                    FROM post_history AS history
                    JOIN accounts ON accounts.id=history.account_id
                    WHERE accounts.workspace_id=?
                    ORDER BY history.published_at DESC, history.id DESC
                    LIMIT ?
                    """,
                    (self.workspace.get(), limit),
                )
            ]

    def release_publish(self, post_id, *, error, retry_kind, retry_at=None):
        """Undo a publish claim after the provider definitively did not publish.

        The job returns to ``ready`` (the container is still valid) and the receipt
        records the refusal plus how to retry. Both happen in one transaction, and
        the receipt is only released from a claimed/uncertain state, so a concurrent
        reconciliation or a second publish cannot be clobbered.

        ``failure_stage='publish'`` is recorded because the container was proven
        usable (it was claimed and dispatched): only the publish call was refused.
        """
        now = int(time.time())
        with self.connection() as db:
            released = db.execute(
                """
                UPDATE publishing_attempts
                SET status='failed', error=?, retry_kind=?, retry_at=?,
                    failure_stage='publish', claimed_at=NULL, updated_at=?
                WHERE post_id=? AND status IN ('submitted', 'unknown')
                """,
                (error, retry_kind, retry_at, now, post_id),
            ).rowcount
            if released:
                db.execute(
                    "UPDATE instagram_posts SET status='ready' "
                    "WHERE id=? AND status='publishing'",
                    (post_id,),
                )
        return released == 1

    def reopen_failed_publish(self, post_id):
        """Re-arm a definitively failed publish for one explicit retry, atomically.

        Both the job and its receipt return to a dispatchable state in one
        transaction, so no concurrent reader can see a ``ready`` job beside a
        ``failed`` receipt (or the reverse). Only ``failed`` is accepted:
        ``unknown`` may have published and must never be re-sent, and
        ``publishing`` is already claimed by another request.
        """
        now = int(time.time())
        with self.connection() as db:
            reopened = db.execute(
                "UPDATE instagram_posts SET status='ready' WHERE id=? AND status='failed'",
                (post_id,),
            ).rowcount
            if not reopened:
                return False
            db.execute(
                """
                UPDATE publishing_attempts
                SET status='pending', claimed_at=NULL, error=NULL,
                    retry_kind=NULL, retry_at=NULL, failure_stage=NULL, updated_at=?
                WHERE post_id=? AND status='failed'
                """,
                (now, post_id),
            )
        return True

    def claim_post(self, post_id, publish_key=None, publish_fingerprint=None):
        """Durably claim a ready job before the publish call.

        The claim and the receipt's ``submitted`` status are written in one
        transaction, so a crash after this returns leaves a durable record that a
        publish may have been dispatched. A job that is not ``ready`` (including
        one already ``publishing`` or ``unknown``) is never re-claimed. An explicit
        retry first moves a ``failed`` job back to ``ready`` in its own guarded
        write, so this remains a single, narrow transition.
        """
        with self.connection() as db:
            claimed = (
                db.execute(
                    """
                UPDATE instagram_posts SET status='publishing' WHERE id=? AND status='ready'
            """,
                    (post_id,),
                ).rowcount
                == 1
            )
            if claimed:
                self._mirror_attempt(db, post_id, "publishing")
                db.execute(
                    """
                    UPDATE publishing_attempts
                    SET publish_idempotency_key=COALESCE(?, publish_idempotency_key),
                        publish_fingerprint=COALESCE(?, publish_fingerprint)
                    WHERE post_id=?
                    """,
                    (publish_key, publish_fingerprint, post_id),
                )
        return claimed
