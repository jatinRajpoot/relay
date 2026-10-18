"""Account credentials, revocable browser sessions, and workspace ownership."""

import hashlib
import secrets
import sqlite3
import time

from fastapi import HTTPException

from relay.store import digest

SESSION_SECONDS = 8 * 3600


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    derived = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=5)
    return f"{salt}:{derived.hex()}"


class Identity:
    def __init__(self, store):
        self.store = store
        self.dummy_hash = password_hash(secrets.token_urlsafe(32))
        with store.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL, password_hash TEXT NOT NULL, created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id),
                    name TEXT NOT NULL, created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id), expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_attempts (
                    bucket TEXT NOT NULL, created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS auth_attempt_bucket ON auth_attempts(bucket, created_at);
            """)

    @staticmethod
    def _buckets(address, email):
        # A shared address gets a looser limit than a single email address.
        return ((digest(f"ip:{address}"), 30), (digest(f"email:{email}"), 10))

    def throttle(self, address, email):
        """Refuse a new attempt once this address or email has failed too often recently."""
        now = int(time.time())
        with self.store.connection() as db:
            db.execute("DELETE FROM auth_attempts WHERE created_at<?", (now - 900,))
            for bucket, limit in self._buckets(address, email):
                count = db.execute(
                    "SELECT COUNT(*) FROM auth_attempts WHERE bucket=?", (bucket,)
                ).fetchone()[0]
                if count >= limit:
                    raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")

    def record_failure(self, address, email):
        """Count a failed attempt. Successful sign-ins never consume the budget."""
        now = int(time.time())
        with self.store.connection() as db:
            db.executemany(
                "INSERT INTO auth_attempts(bucket, created_at) VALUES (?, ?)",
                [(bucket, now) for bucket, _ in self._buckets(address, email)],
            )

    def signup(self, email, password, name, workspace_name):
        user_id, workspace_id = secrets.token_hex(16), secrets.token_hex(16)
        encoded = password_hash(password)
        try:
            with self.store.connection() as db:
                db.execute(
                    "INSERT INTO users VALUES (?, ?, ?, ?, ?)",
                    (user_id, email, name, encoded, int(time.time())),
                )
                db.execute(
                    "INSERT INTO workspaces VALUES (?, ?, ?, ?)",
                    (workspace_id, user_id, workspace_name, int(time.time())),
                )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Unable to create this account. Try logging in instead.") from None
        return user_id, workspace_id

    def login(self, email, password):
        with self.store.connection() as db:
            user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            encoded = user["password_hash"] if user else self.dummy_hash
            valid = secrets.compare_digest(password_hash(password, encoded.split(":")[0]), encoded)
            if not user or not valid:
                raise HTTPException(401, "Email or password is incorrect.")
            workspace = db.execute(
                "SELECT id FROM workspaces WHERE owner_id=? ORDER BY created_at, id LIMIT 1",
                (user["id"],),
            ).fetchone()
        return user["id"], workspace["id"]

    def new_session(self, user_id, workspace_id, previous_token=""):
        token = secrets.token_urlsafe(32)
        with self.store.connection() as db:
            db.execute("DELETE FROM user_sessions WHERE expires_at<=? OR token_hash=?",
                       (int(time.time()), digest(previous_token)))
            db.execute("INSERT INTO user_sessions VALUES (?, ?, ?, ?)",
                       (digest(token), user_id, workspace_id, int(time.time()) + SESSION_SECONDS))
        return token

    def session(self, token, workspace_id=None):
        if not token:
            return None
        with self.store.connection() as db:
            row = db.execute(
                """SELECT u.id, u.email, u.name, s.workspace_id FROM user_sessions s
                   JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?""",
                (digest(token), int(time.time())),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            if workspace_id:
                result["workspace_id"] = workspace_id
            workspace = db.execute(
                "SELECT id, name FROM workspaces WHERE id=? AND owner_id=?",
                (result["workspace_id"], result["id"]),
            ).fetchone()
            if workspace is None:
                raise HTTPException(404, "Workspace not found.")
            result["workspace"] = dict(workspace)
            return result

    def workspaces(self, user_id):
        with self.store.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT id, name FROM workspaces WHERE owner_id=? ORDER BY created_at, id",
                (user_id,),
            )]

    def create_workspace(self, user_id, name):
        workspace_id = secrets.token_hex(16)
        with self.store.connection() as db:
            count = db.execute("SELECT COUNT(*) FROM workspaces WHERE owner_id=?",
                               (user_id,)).fetchone()[0]
            if count >= 50:
                raise HTTPException(409, "This account has reached its limit of 50 workspaces.")
            db.execute("INSERT INTO workspaces VALUES (?, ?, ?, ?)",
                       (workspace_id, user_id, name, int(time.time())))
        return {"id": workspace_id, "name": name}

    def select_workspace(self, token, workspace_id):
        # This also checks ownership before changing the browser's selected workspace.
        self.session(token, workspace_id)
        with self.store.connection() as db:
            db.execute("UPDATE user_sessions SET workspace_id=? WHERE token_hash=?",
                       (workspace_id, digest(token)))

    def logout(self, token):
        with self.store.connection() as db:
            db.execute("DELETE FROM user_sessions WHERE token_hash=?", (digest(token),))
