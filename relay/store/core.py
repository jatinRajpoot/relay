import hashlib
import os
import sqlite3
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from cryptography.fernet import Fernet


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class StoreCore:
    def __init__(self, path: Path, encryption_key: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.cipher = Fernet(encryption_key.encode())
        # Set by the authentication dependency, isolated for each async request.
        self.workspace = ContextVar("relay_workspace", default="legacy")
        self._lock = threading.RLock()
        self._blob = None
        if bucket := os.getenv("DATABASE_BUCKET"):
            from google.cloud import storage

            self._blob = storage.Client().bucket(bucket).blob("relay.sqlite3")
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
                    UNIQUE(provider, platform_post_id)
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
                ON CONFLICT(provider, platform_post_id) DO NOTHING
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS post_history_published_at "
                "ON post_history(published_at DESC, id DESC)"
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

    @contextmanager
    def connection(self):
        with self._lock, self._connection() as db:
            yield db

    @contextmanager
    def _connection(self):
        generation = 0
        before = b""
        if self._blob is not None:
            from google.api_core.exceptions import NotFound

            try:
                self._blob.reload()
                generation = int(self._blob.generation)
                before = self._blob.download_as_bytes(if_generation_match=generation)
            except NotFound:
                pass
            self.path.write_bytes(before)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()
        if self._blob is not None:
            after = self.path.read_bytes()
            if after != before:
                # A stale revision must fail rather than overwrite a newer transaction.
                self._blob.upload_from_string(
                    after, content_type="application/octet-stream",
                    if_generation_match=generation,
                )

    def encrypt(self, value: str | None) -> str | None:
        return self.cipher.encrypt(value.encode()).decode() if value else None

    def decrypt(self, value: str | None) -> str | None:
        return self.cipher.decrypt(value.encode()).decode() if value else None
