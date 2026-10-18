import secrets
import time


class AccountStore:
    def save_account(self, provider, external_id, name, tokens, scopes, avatar_url=None):
        now = int(time.time())
        with self.connection() as db:
            db.execute(
                """
                INSERT INTO accounts (
                    id, provider, external_id, name, access_token, refresh_token,
                    expires_at, scopes, token_updated_at, created_at, avatar_url, workspace_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace_id, provider, external_id) DO UPDATE SET
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
                    self.workspace.get(),
                ),
            )
            row = db.execute(
                "SELECT * FROM accounts WHERE provider=? AND external_id=? AND workspace_id=?",
                (provider, str(external_id), self.workspace.get()),
            ).fetchone()
        return dict(row)

    def get_account(self, account_id):
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM accounts WHERE id=? AND workspace_id=?",
                (account_id, self.workspace.get()),
            ).fetchone()
        return dict(row) if row else None

    def list_accounts(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM accounts WHERE workspace_id=? ORDER BY created_at",
                (self.workspace.get(),),
            )]

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
            return db.execute(
                "DELETE FROM accounts WHERE id=? AND workspace_id=?",
                (account_id, self.workspace.get()),
            ).rowcount > 0

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
