import hashlib
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, path: Path, encryption_key: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.cipher = Fernet(encryption_key.encode())
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
                    UNIQUE(provider, external_id)
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

    def create_state(
        self,
        provider: str,
        browser: str,
        verifier: str | None,
        return_to_ui: bool = False,
        popup: bool = False,
    ) -> str:
        state = secrets.token_urlsafe(32)
        now = int(time.time())
        with self.connection() as db:
            db.execute("DELETE FROM oauth_states WHERE expires_at <= ?", (now,))
            db.execute(
                "INSERT INTO oauth_states(state_hash, provider, browser_hash, verifier, expires_at, return_to_ui, popup) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    digest(state),
                    provider,
                    digest(browser),
                    self.encrypt(verifier),
                    now + 600,
                    int(return_to_ui),
                    int(popup),
                ),
            )
        return state

    def consume_state(self, state: str, provider: str, browser: str):
        with self.connection() as db:
            row = db.execute(
                """
                DELETE FROM oauth_states
                WHERE state_hash=? AND provider=? AND browser_hash=? AND expires_at>?
                RETURNING verifier, return_to_ui, popup
            """,
                (digest(state), provider, digest(browser), int(time.time())),
            ).fetchone()
        return (
            {
                "verifier": self.decrypt(row["verifier"]),
                "return_to_ui": bool(row["return_to_ui"]),
                "popup": bool(row["popup"]),
            }
            if row
            else None
        )

    def save_account(self, provider, external_id, name, tokens, scopes, avatar_url=None):
        now = int(time.time())
        with self.connection() as db:
            db.execute(
                """
                INSERT INTO accounts (
                    id, provider, external_id, name, access_token, refresh_token,
                    expires_at, scopes, token_updated_at, created_at, avatar_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, external_id) DO UPDATE SET
                    name=excluded.name, access_token=excluded.access_token,
                    refresh_token=COALESCE(excluded.refresh_token, accounts.refresh_token),
                    expires_at=excluded.expires_at, scopes=excluded.scopes,
                    token_updated_at=excluded.token_updated_at,
                    avatar_url=COALESCE(excluded.avatar_url, accounts.avatar_url)
            """,
                (
                    secrets.token_hex(16),
                    provider,
                    str(external_id),
                    name,
                    self.encrypt(tokens["access_token"]),
                    self.encrypt(tokens.get("refresh_token")),
                    now + int(tokens["expires_in"]),
                    scopes,
                    now,
                    now,
                    avatar_url,
                ),
            )
            row = db.execute(
                "SELECT * FROM accounts WHERE provider=? AND external_id=?",
                (provider, str(external_id)),
            ).fetchone()
        return dict(row)

    def get_account(self, account_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return dict(row) if row else None

    def list_accounts(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM accounts ORDER BY created_at")]

    def update_account_avatar(self, account_id, avatar_url):
        with self.connection() as db:
            db.execute(
                "UPDATE accounts SET avatar_url=? WHERE id=? AND avatar_url IS NULL",
                (avatar_url, account_id),
            )
        return self.get_account(account_id)

    def update_tokens(self, account_id, tokens):
        now = int(time.time())
        with self.connection() as db:
            db.execute(
                """
                UPDATE accounts SET access_token=?, refresh_token=COALESCE(?, refresh_token),
                    expires_at=?, token_updated_at=? WHERE id=?
            """,
                (
                    self.encrypt(tokens["access_token"]),
                    self.encrypt(tokens.get("refresh_token")),
                    now + int(tokens["expires_in"]),
                    now,
                    account_id,
                ),
            )
        return self.get_account(account_id)

    def delete_account(self, account_id):
        with self.connection() as db:
            return db.execute("DELETE FROM accounts WHERE id=?", (account_id,)).rowcount > 0

    @staticmethod
    def public_account(account):
        return {
            key: account[key]
            for key in (
                "id",
                "provider",
                "external_id",
                "name",
                "expires_at",
                "scopes",
                "created_at",
                "avatar_url",
            )
        } | {"has_refresh_token": bool(account["refresh_token"])}

    def create_post(self, account_id, container_id, media_type=None, media_url=None, caption=""):
        post_id = secrets.token_hex(16)
        with self.connection() as db:
            db.execute(
                """
                INSERT INTO instagram_posts(
                    id, account_id, container_id, created_at, media_type, media_url, caption
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (post_id, account_id, container_id, int(time.time()), media_type, media_url, caption),
            )
        return self.get_post(post_id)

    def get_post(self, post_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM instagram_posts WHERE id=?", (post_id,)).fetchone()
        return dict(row) if row else None

    def update_post(self, post_id, status, media_id=None):
        with self.connection() as db:
            db.execute(
                """
                UPDATE instagram_posts SET status=?, media_id=COALESCE(?, media_id) WHERE id=?
            """,
                (status, media_id, post_id),
            )
            if status == "published":
                post = db.execute(
                    "SELECT * FROM instagram_posts WHERE id=?", (post_id,)
                ).fetchone()
                if post:
                    self._save_instagram_history(db, post)
        return self.get_post(post_id)

    def sync_post_status(self, post_id, previous_status, status):
        with self.connection() as db:
            updated = db.execute(
                "UPDATE instagram_posts SET status=? WHERE id=? AND status=?",
                (status, post_id, previous_status),
            ).rowcount
            if updated and status == "published":
                post = db.execute(
                    "SELECT * FROM instagram_posts WHERE id=?", (post_id,)
                ).fetchone()
                if post:
                    self._save_instagram_history(db, post)
        return self.get_post(post_id)

    @staticmethod
    def _save_instagram_history(db, post):
        db.execute(
            """
            INSERT INTO post_history(
                id, provider, account_id, platform_post_id, caption, media_type,
                media_url, published_at
            ) VALUES (?, 'instagram', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, platform_post_id) DO NOTHING
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
                ON CONFLICT(provider, platform_post_id) DO NOTHING
                """,
                (
                    f"youtube-{video_id}",
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
                    WITH history_items AS (
                        SELECT
                            'instagram-' || posts.id AS id,
                            'instagram' AS provider,
                            posts.account_id,
                            COALESCE(posts.media_id, posts.container_id) AS platform_post_id,
                            NULL AS title,
                            COALESCE(posts.caption, '') AS caption,
                            NULL AS media_name,
                            posts.media_type,
                            posts.media_url,
                            NULL AS post_url,
                            NULL AS visibility,
                            COALESCE(history.published_at, posts.created_at) AS published_at,
                            posts.status,
                            posts.id AS instagram_post_id,
                            accounts.name AS account_name
                        FROM instagram_posts AS posts
                        JOIN accounts ON accounts.id=posts.account_id
                        LEFT JOIN post_history AS history ON history.id=('instagram-' || posts.id)

                        UNION ALL

                        SELECT
                            history.id,
                            history.provider,
                            history.account_id,
                            history.platform_post_id,
                            history.title,
                            history.caption,
                            history.media_name,
                            history.media_type,
                            history.media_url,
                            history.post_url,
                            history.visibility,
                            history.published_at,
                            'published' AS status,
                            NULL AS instagram_post_id,
                            accounts.name AS account_name
                        FROM post_history AS history
                        JOIN accounts ON accounts.id=history.account_id
                        WHERE history.provider='youtube'
                    )
                    SELECT * FROM history_items
                    ORDER BY published_at DESC, id DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            ]

    def claim_post(self, post_id):
        with self.connection() as db:
            return (
                db.execute(
                    """
                UPDATE instagram_posts SET status='publishing' WHERE id=? AND status='ready'
            """,
                    (post_id,),
                ).rowcount
                == 1
            )

    def create_content_item(self, filename, content_type, size, object_name, media_url):
        content_id = secrets.token_hex(16)
        created_at = int(time.time())
        with self.connection() as db:
            db.execute(
                """
                INSERT INTO content_items(id, filename, content_type, size, object_name, media_url, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (content_id, filename, content_type, size, object_name, media_url, created_at),
            )
        return self.get_content_item(content_id)

    def get_content_item(self, content_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM content_items WHERE id=?", (content_id,)).fetchone()
        return dict(row) if row else None

    def list_content_items(self):
        with self.connection() as db:
            return [
                dict(row)
                for row in db.execute("SELECT * FROM content_items ORDER BY created_at DESC, id DESC")
            ]
