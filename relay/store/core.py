import hashlib
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from cryptography.fernet import Fernet

_SQLITE_MAGIC = b"SQLite format 3\x00"
_READ_ATTEMPTS = 3


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class StorageError(RuntimeError):
    """The durable snapshot could not be read or written.

    ``outcome`` states what is known about the change the caller attempted:

    ``not_applied``  the remote snapshot definitely does not contain it
    ``unknown``      it may or may not have been applied

    Routes map this to a sanitised 503 rather than a raw provider/Google error.
    """

    def __init__(self, detail: str, *, outcome: str = "unknown"):
        super().__init__(detail)
        self.detail = detail
        self.outcome = outcome


def _storage_conflict(detail: str, cause):
    """A lost generation compare-and-swap.

    It is both a ``StorageError`` (so routes answer a sanitised 503 with an
    explicit outcome) and a ``PreconditionFailed`` (so callers that already
    distinguish a lost CAS keep working unchanged).
    """
    from google.api_core.exceptions import PreconditionFailed

    class StorageConflict(StorageError, PreconditionFailed):
        pass

    error = StorageConflict(detail, outcome="unknown")
    error.__cause__ = cause
    return error


_UNSET = object()


def _cloud_blob():
    """The configured snapshot blob, or ``None`` when this is a local store."""
    bucket = os.getenv("DATABASE_BUCKET", "").strip()
    if not bucket:
        return None
    from google.cloud import storage

    return storage.Client().bucket(bucket).blob("relay.sqlite3")


class StoreCore:
    def __init__(self, path: Path, encryption_key: str, *, blob=_UNSET):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.cipher = Fernet(encryption_key.encode())
        # Set by the authentication dependency, isolated for each async request.
        self.workspace = ContextVar("relay_workspace", default="legacy")
        self._lock = threading.RLock()
        self._blob = _cloud_blob() if blob is _UNSET else blob
        # A configured cloud database is never created here. ``initialize_cloud_snapshot``
        # is the only creation path, so a missing object fails closed at startup instead
        # of being silently replaced with an empty database.
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=DELETE" if self._blob else "PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS oauth_states (
                    state_hash TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    browser_hash TEXT NOT NULL,
                    verifier TEXT,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL CHECK(provider IN ('youtube', 'instagram')),
                    external_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    access_token TEXT NOT NULL,
                    refresh_token TEXT,
                    expires_at INTEGER NOT NULL,
                    scopes TEXT NOT NULL,
                    token_updated_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    workspace_id TEXT NOT NULL DEFAULT 'legacy',
                    UNIQUE(workspace_id, provider, external_id)
                );
                CREATE TABLE IF NOT EXISTS instagram_posts (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    container_id TEXT NOT NULL UNIQUE,
                    media_id TEXT,
                    status TEXT NOT NULL DEFAULT 'processing',
                    created_at INTEGER NOT NULL,
                    media_type TEXT,
                    media_url TEXT,
                    caption TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS post_history (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL CHECK(provider IN ('youtube', 'instagram')),
                    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    platform_post_id TEXT NOT NULL,
                    title TEXT,
                    caption TEXT NOT NULL DEFAULT '',
                    media_name TEXT,
                    media_type TEXT,
                    media_url TEXT,
                    post_url TEXT,
                    visibility TEXT,
                    published_at INTEGER NOT NULL,
                    UNIQUE(provider, account_id, platform_post_id)
                );
                CREATE TABLE IF NOT EXISTS content_items (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    object_name TEXT NOT NULL UNIQUE,
                    media_url TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS publishing_attempts (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    provider TEXT NOT NULL CHECK(provider IN ('youtube', 'instagram')),
                    status TEXT NOT NULL,
                    post_id TEXT REFERENCES instagram_posts(id) ON DELETE CASCADE,
                    provider_ref TEXT,
                    provider_url TEXT,
                    media_name TEXT,
                    media_type TEXT,
                    media_url TEXT,
                    title TEXT,
                    caption TEXT NOT NULL DEFAULT '',
                    privacy_status TEXT,
                    idempotency_key TEXT,
                    request_fingerprint TEXT,
                    publish_idempotency_key TEXT,
                    publish_fingerprint TEXT,
                    error TEXT,
                    retry_kind TEXT,
                    retry_at INTEGER,
                    failure_stage TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    claimed_at INTEGER
                );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(oauth_states)")}
            if "return_to_ui" not in columns:
                db.execute(
                    "ALTER TABLE oauth_states ADD COLUMN return_to_ui INTEGER NOT NULL DEFAULT 0"
                )
            if "popup" not in columns:
                db.execute("ALTER TABLE oauth_states ADD COLUMN popup INTEGER NOT NULL DEFAULT 0")
            account_columns = {row[1] for row in db.execute("PRAGMA table_info(accounts)")}
            if "avatar_url" not in account_columns:
                db.execute("ALTER TABLE accounts ADD COLUMN avatar_url TEXT")
            self._migrate_workspaces(db)
            self._migrate_post_history_key(db)
            post_columns = {row[1] for row in db.execute("PRAGMA table_info(instagram_posts)")}
            for column, declaration in (
                ("media_type", "TEXT"),
                ("media_url", "TEXT"),
                ("caption", "TEXT NOT NULL DEFAULT ''"),
            ):
                if column not in post_columns:
                    db.execute(f"ALTER TABLE instagram_posts ADD COLUMN {column} {declaration}")
            db.execute(
                """
                INSERT INTO post_history(
                    id, provider, account_id, platform_post_id, published_at
                )
                SELECT 'instagram-' || id, 'instagram', account_id, media_id, created_at
                FROM instagram_posts
                WHERE status='published' AND media_id IS NOT NULL
                ON CONFLICT DO NOTHING
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS post_history_published_at "
                "ON post_history(published_at DESC, id DESC)"
            )
            attempt_columns = {
                row[1] for row in db.execute("PRAGMA table_info(publishing_attempts)")
            }
            for column, declaration in (
                ("publish_idempotency_key", "TEXT"),
                ("publish_fingerprint", "TEXT"),
                ("claimed_at", "INTEGER"),
                ("retry_kind", "TEXT"),
                ("retry_at", "INTEGER"),
                ("failure_stage", "TEXT"),
            ):
                if column not in attempt_columns:
                    db.execute(
                        f"ALTER TABLE publishing_attempts ADD COLUMN {column} {declaration}"
                    )
            db.executescript("""
                CREATE INDEX IF NOT EXISTS publishing_attempts_page
                    ON publishing_attempts(workspace_id, created_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS publishing_attempts_post
                    ON publishing_attempts(post_id);
                CREATE UNIQUE INDEX IF NOT EXISTS publishing_attempts_idempotency
                    ON publishing_attempts(provider, account_id, idempotency_key)
                    WHERE idempotency_key IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS publishing_attempts_publish_idempotency
                    ON publishing_attempts(provider, account_id, publish_idempotency_key)
                    WHERE publish_idempotency_key IS NOT NULL;
            """)
            self._backfill_publishing_attempts(db)

    @staticmethod
    def _backfill_publishing_attempts(db):
        """Give existing Instagram jobs and known successes a recovery receipt.

        Additive and idempotent: deterministic ids plus ``WHERE NOT EXISTS`` mean a
        re-open never duplicates a row. Only states the database actually recorded
        are materialised — no historical failure is invented, and no draft is
        created. A legacy ``publishing`` job is backfilled as ``unknown`` because
        the claim was taken and no outcome was stored.
        """
        db.execute(
            """
            INSERT INTO publishing_attempts(
                id, workspace_id, account_id, provider, status, post_id,
                provider_ref, media_type, media_url, caption, error,
                created_at, updated_at, claimed_at
            )
            SELECT 'ig-' || p.id, a.workspace_id, p.account_id, 'instagram',
                   CASE p.status
                       WHEN 'published' THEN 'published'
                       WHEN 'failed' THEN 'failed'
                       WHEN 'expired' THEN 'expired'
                       WHEN 'unknown' THEN 'unknown'
                       WHEN 'publishing' THEN 'unknown'
                       WHEN 'ready' THEN 'ready'
                       ELSE 'processing'
                   END,
                   p.id,
                   COALESCE(p.media_id, p.container_id),
                   p.media_type, p.media_url, COALESCE(p.caption, ''),
                   CASE p.status
                       WHEN 'unknown' THEN 'This publish outcome was never confirmed. '
                                            || 'Verify it before retrying.'
                       WHEN 'publishing' THEN 'This publish was interrupted before the '
                                              || 'provider answered. Verify it before retrying.'
                       ELSE NULL
                   END,
                   p.created_at, p.created_at,
                   CASE p.status WHEN 'publishing' THEN p.created_at ELSE NULL END
            FROM instagram_posts p
            JOIN accounts a ON a.id=p.account_id
            WHERE NOT EXISTS (SELECT 1 FROM publishing_attempts x WHERE x.post_id=p.id)
            """
        )
        db.execute(
            """
            INSERT INTO publishing_attempts(
                id, workspace_id, account_id, provider, status, provider_ref,
                provider_url, media_name, media_type, media_url, title, caption,
                privacy_status, created_at, updated_at
            )
            SELECT 'hist-' || h.id, a.workspace_id, h.account_id, h.provider, 'published',
                   h.platform_post_id, h.post_url, h.media_name, h.media_type, h.media_url,
                   h.title, COALESCE(h.caption, ''), h.visibility,
                   h.published_at, h.published_at
            FROM post_history h
            JOIN accounts a ON a.id=h.account_id
            WHERE NOT EXISTS (
                SELECT 1 FROM publishing_attempts x
                WHERE x.provider=h.provider AND x.account_id=h.account_id
                  AND x.provider_ref=h.platform_post_id
            )
            AND NOT EXISTS (
                SELECT 1 FROM publishing_attempts x
                WHERE x.id='hist-' || h.id
            )
            """
        )

    @staticmethod
    def _migrate_workspaces(db):
        # Rebuild the old unique constraint without cascading away existing jobs/history.
        columns = {row[1] for row in db.execute("PRAGMA table_info(accounts)")}
        if "workspace_id" not in columns:
            db.commit()
            db.execute("PRAGMA foreign_keys=OFF")
            try:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("""CREATE TABLE accounts_scoped (
                        id TEXT PRIMARY KEY,
                        provider TEXT NOT NULL CHECK(provider IN ('youtube', 'instagram')),
                        external_id TEXT NOT NULL, name TEXT NOT NULL,
                        access_token TEXT NOT NULL, refresh_token TEXT,
                        expires_at INTEGER NOT NULL, scopes TEXT NOT NULL,
                        token_updated_at INTEGER NOT NULL, created_at INTEGER NOT NULL,
                        avatar_url TEXT, workspace_id TEXT NOT NULL DEFAULT 'legacy',
                        UNIQUE(workspace_id, provider, external_id)
                    )""")
                    db.execute("""INSERT INTO accounts_scoped
                        SELECT id, provider, external_id, name, access_token, refresh_token,
                        expires_at, scopes, token_updated_at, created_at, avatar_url, 'legacy'
                        FROM accounts""")
                    db.execute("DROP TABLE accounts")
                    db.execute("ALTER TABLE accounts_scoped RENAME TO accounts")
            finally:
                db.execute("PRAGMA foreign_keys=ON")
        for table in ("content_items", "oauth_states"):
            columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            if "workspace_id" not in columns:
                db.execute(
                    f"ALTER TABLE {table} ADD COLUMN workspace_id TEXT NOT NULL DEFAULT 'legacy'"
                )

    @staticmethod
    def _migrate_post_history_key(db):
        """Rebuild post_history so its unique key includes the connected account.

        Older releases keyed history by ``(provider, platform_post_id)`` alone, so a
        second account recording the same platform id was silently dropped by the
        ``ON CONFLICT DO NOTHING`` insert. The rebuild copies every existing row, keeps
        foreign keys intact, and is a no-op once the new key is in place.
        """
        legacy = False
        for index in db.execute("PRAGMA index_list(post_history)").fetchall():
            if not index[2]:  # only UNIQUE indexes
                continue
            columns = [row[2] for row in db.execute(f'PRAGMA index_info("{index[1]}")')]
            if columns == ["provider", "platform_post_id"]:
                legacy = True
        if not legacy:
            return
        # Copy only columns both shapes share, so a database carrying extra columns is
        # never silently truncated by this rebuild.
        new_columns = [
            "id", "provider", "account_id", "platform_post_id", "title", "caption",
            "media_name", "media_type", "media_url", "post_url", "visibility",
            "published_at",
        ]
        existing = {row[1] for row in db.execute("PRAGMA table_info(post_history)")}
        copied = ", ".join(column for column in new_columns if column in existing)
        db.commit()
        db.execute("PRAGMA foreign_keys=OFF")
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("""CREATE TABLE post_history_scoped (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL CHECK(provider IN ('youtube', 'instagram')),
                    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    platform_post_id TEXT NOT NULL,
                    title TEXT,
                    caption TEXT NOT NULL DEFAULT '',
                    media_name TEXT,
                    media_type TEXT,
                    media_url TEXT,
                    post_url TEXT,
                    visibility TEXT,
                    published_at INTEGER NOT NULL,
                    UNIQUE(provider, account_id, platform_post_id)
                )""")
                db.execute(
                    f"INSERT INTO post_history_scoped({copied}) SELECT {copied} FROM post_history"
                )
                db.execute("DROP TABLE post_history")
                db.execute("ALTER TABLE post_history_scoped RENAME TO post_history")
        finally:
            db.execute("PRAGMA foreign_keys=ON")

    # ── Cloud snapshot I/O ───────────────────────────────────────────────
    @classmethod
    def initialize_cloud_snapshot(cls, path: Path, encryption_key: str, *, blob=None):
        """Create the first cloud snapshot, and only ever the first.

        This is the **only** code path that creates a cloud database. Normal
        ``Store`` construction never does: a configured bucket whose object is
        missing fails closed, because a deleted live snapshot and a first boot
        are indistinguishable from inside a Cloud Run instance.

        The snapshot is built locally with the full current schema, validated,
        then uploaded with ``if_generation_match=0`` — a create-if-absent
        compare-and-swap. If a snapshot already exists the call changes nothing
        and returns ``False``; it can never replace live data. Returns ``True``
        when it created the object.

        Callers must be explicit: this is a one-shot operator action, exposed
        through ``scripts/init_cloud_database.py``.
        """
        from google.api_core.exceptions import GoogleAPIError, PreconditionFailed

        if blob is None:
            blob = _cloud_blob()
        if blob is None:
            raise StorageError(
                "DATABASE_BUCKET is not configured; there is no cloud database to initialize.",
                outcome="not_applied",
            )
        staged = path.with_name(f"{path.name}.init")
        sidecars = [Path(f"{staged}{suffix}") for suffix in ("-journal", "-wal", "-shm")]
        try:
            for leftover in (staged, *sidecars):
                if leftover.exists():
                    leftover.unlink()
            # Build the schema with the same migration code the service runs, so the
            # first snapshot is immediately usable rather than a bare file that the
            # next boot has to migrate (and then re-upload).
            cls(staged, encryption_key, blob=None)
            # Normal local stores use WAL. A snapshot must be a single self-contained
            # file: checkpoint and switch back to DELETE before reading the bytes.
            db = sqlite3.connect(staged)
            try:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                db.execute("PRAGMA journal_mode=DELETE")
            finally:
                db.close()
            for leftover in sidecars:
                if leftover.exists():
                    leftover.unlink()
            payload = staged.read_bytes()
            if not payload.startswith(_SQLITE_MAGIC) or not cls._is_sqlite(staged):
                raise StorageError(
                    "Could not build a valid initial database.", outcome="not_applied"
                )
            try:
                blob.upload_from_string(
                    payload,
                    content_type="application/octet-stream",
                    if_generation_match=0,
                )
            except PreconditionFailed:
                # A snapshot already exists. Initialization is a no-op, never a replace.
                return False
            return True
        except StorageError:
            raise
        except (GoogleAPIError, OSError, sqlite3.Error) as error:
            raise StorageError(
                "The cloud database could not be initialized.", outcome="not_applied"
            ) from error
        finally:
            for leftover in (staged, *sidecars):
                if leftover.exists():
                    leftover.unlink()

    def _staged_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.download")

    @staticmethod
    def _is_sqlite(path: Path) -> bool:
        """Whether a downloaded file is a readable SQLite database.

        A magic-number check alone accepts a truncated object; opening it and
        running ``quick_check`` is what proves the snapshot can actually serve
        requests. ``quick_check`` is used rather than ``integrity_check`` because
        this runs on every snapshot read and the full check is markedly slower.
        It runs only on a staged copy, never on the live cache.
        """
        try:
            db = sqlite3.connect(path)
            try:
                result = db.execute("PRAGMA quick_check").fetchone()
            finally:
                db.close()
        except sqlite3.Error:
            return False
        return bool(result) and result[0] == "ok"

    def _download(self):
        """Read the current snapshot, pinned to one generation.

        ``reload`` then ``download_as_bytes(if_generation_match=generation)`` makes
        the bytes and the generation provably the same revision: if another
        instance writes in between, the download fails its precondition and we
        retry rather than mixing a new generation with old bytes. A missing object
        is never retried into existence — that is the fail-closed signal.
        """
        from google.api_core.exceptions import GoogleAPIError, NotFound, PreconditionFailed

        last_error = None
        for attempt in range(_READ_ATTEMPTS):
            if attempt:
                time.sleep(0.05 * attempt)
            try:
                self._blob.reload()
                generation = int(self._blob.generation)
                return generation, self._blob.download_as_bytes(if_generation_match=generation)
            except NotFound:
                raise StorageError(
                    "The configured cloud database is missing. Restore it or run the "
                    "explicit initialization step before starting the service.",
                    outcome="not_applied",
                ) from None
            except PreconditionFailed as error:
                # Another writer moved the generation between reload and download.
                last_error = error
                continue
            except (GoogleAPIError, OSError) as error:
                last_error = error
                continue
        raise StorageError(
            "The cloud database is temporarily unavailable.", outcome="not_applied"
        ) from last_error

    def _read_remote(self):
        """Download the current snapshot into a validated staged file.

        Returns ``(generation, bytes)``. The local cache is replaced atomically and
        only after the download is proven to be a SQLite database, so an empty or
        corrupt object can never truncate the diagnostic copy already on disk. A
        journal left by an earlier local file is removed *before* the replace, so
        it can never be replayed onto the freshly downloaded snapshot.
        """
        generation, data = self._download()
        if not data or not data.startswith(_SQLITE_MAGIC):
            raise StorageError(
                "The cloud database snapshot is empty or corrupt. It was not loaded.",
                outcome="not_applied",
            )
        staged = self._staged_path()
        try:
            staged.write_bytes(data)
        except OSError as error:
            raise StorageError(
                "The cloud database could not be staged locally.", outcome="not_applied"
            ) from error
        try:
            if not self._is_sqlite(staged):
                # A header alone proves nothing: a truncated or corrupt object must
                # not replace the local diagnostic copy or be served as the database.
                raise StorageError(
                    "The cloud database snapshot is corrupt. It was not loaded.",
                    outcome="not_applied",
                )
            # Remove stale sidecars first: if one cannot be removed, the old cache
            # stays in place rather than risking a journal replay onto new bytes.
            self._remove_sidecars()
            os.replace(staged, self.path)
        except StorageError:
            raise
        except OSError as error:
            raise StorageError(
                "The cloud database could not be staged locally.", outcome="not_applied"
            ) from error
        finally:
            if staged.exists():
                staged.unlink()
        return generation, data

    def _remove_sidecars(self):
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = Path(f"{self.path}{suffix}")
            try:
                if sidecar.exists():
                    sidecar.unlink()
            except OSError as error:
                raise StorageError(
                    "The local database cache is locked and could not be refreshed.",
                    outcome="not_applied",
                ) from error

    def _write_remote(self, generation, after):
        """Upload a changed snapshot with a generation compare-and-swap."""
        from google.api_core.exceptions import GoogleAPIError, PreconditionFailed

        try:
            self._blob.upload_from_string(
                after,
                content_type="application/octet-stream",
                if_generation_match=generation,
            )
            return
        except PreconditionFailed as error:
            # Someone else wrote a newer snapshot. Never overwrite it.
            raise _storage_conflict(
                "A newer database snapshot was written by another instance.", error
            ) from error
        except (GoogleAPIError, OSError) as error:
            upload_error = error
        # The upload may have been accepted even though the response was lost.
        # A bounded reload can distinguish "definitely not stored" from "stored"
        # from "someone else's snapshot".
        for attempt in range(_READ_ATTEMPTS):
            if attempt:
                time.sleep(0.05 * attempt)
            try:
                self._blob.reload()
                current = int(self._blob.generation)
                if current == generation:
                    # The object is untouched: our bytes were definitely not stored.
                    raise StorageError(
                        "The database change could not be saved. It was not applied.",
                        outcome="not_applied",
                    ) from upload_error
                stored = self._blob.download_as_bytes(if_generation_match=current)
            except StorageError:
                raise
            except (GoogleAPIError, OSError):
                continue
            if stored == after:
                # Exactly the bytes we intended are durable at a later generation.
                return
            raise StorageError(
                "The database change may or may not have been saved.",
                outcome="unknown",
            ) from upload_error
        raise StorageError(
            "The database change may or may not have been saved.", outcome="unknown"
        ) from upload_error

    @contextmanager
    def connection(self):
        with self._lock, self._connection() as db:
            yield db

    @contextmanager
    def _connection(self):
        generation = 0
        before = b""
        if self._blob is not None:
            generation, before = self._read_remote()
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            # Close before reading the file: an open handle (and an uncheckpointed
            # journal) would make the bytes we upload differ from what was committed.
            db.close()
        if self._blob is not None:
            after = self._read_committed()
            if after != before:
                # A stale revision must fail rather than overwrite a newer transaction.
                self._write_remote(generation, after)

    def _read_committed(self):
        """The exact committed database bytes, with no journal still pending.

        ``journal_mode=DELETE`` is set for cloud stores, so a successful commit
        removes its journal and the main file is self-contained. If a journal is
        still present the file is not safe to upload: report it rather than send a
        snapshot that silently omits or double-applies a transaction.
        """
        journal = Path(f"{self.path}-journal")
        if journal.exists() and journal.stat().st_size:
            raise StorageError(
                "The database change could not be confirmed locally. It was not saved.",
                outcome="not_applied",
            )
        try:
            return self.path.read_bytes()
        except OSError as error:
            raise StorageError(
                "The database change could not be read back. It may or may not have been saved.",
                outcome="unknown",
            ) from error

    def encrypt(self, value: str | None) -> str | None:
        return self.cipher.encrypt(value.encode()).decode() if value else None

    def decrypt(self, value: str | None) -> str | None:
        return self.cipher.decrypt(value.encode()).decode() if value else None
