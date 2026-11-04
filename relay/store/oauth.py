from __future__ import annotations

import secrets
import time

from cryptography.fernet import InvalidToken

from .core import digest


class OAuthStateStore:
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
                "INSERT INTO oauth_states(state_hash, provider, browser_hash, verifier, expires_at, return_to_ui, popup, workspace_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    digest(state),
                    provider,
                    digest(browser),
                    self.encrypt(verifier),
                    now + 600,
                    int(return_to_ui),
                    int(popup),
                    self.workspace.get(),
                ),
            )
        return state

    def consume_state(self, state: str, provider: str, browser: str):
        """Consume a state row exactly once, or return ``None``.

        The row is deleted by the same statement that reads it, so a replay can never
        succeed even if the verifier turns out to be unreadable. An unreadable verifier
        (a rotated ``TOKEN_ENCRYPTION_KEY``, or a database restored under another key)
        is reported as an unusable state rather than raising: the caller already has a
        recovery path for an expired flow, and a 500 would both leak that something
        decrypts and strand the browser. The ciphertext is never returned or logged.
        """
        with self.connection() as db:
            row = db.execute(
                """
                DELETE FROM oauth_states
                WHERE state_hash=? AND provider=? AND browser_hash=? AND expires_at>?
                RETURNING verifier, return_to_ui, popup, workspace_id
            """,
                (digest(state), provider, digest(browser), int(time.time())),
            ).fetchone()
        if row is None:
            return None
        try:
            verifier = self.decrypt(row["verifier"])
        except InvalidToken:
            return None
        return {
            "verifier": verifier,
            "return_to_ui": bool(row["return_to_ui"]),
            "popup": bool(row["popup"]),
            **({"workspace_id": row["workspace_id"]} if row["workspace_id"] != "legacy" else {}),
        }
