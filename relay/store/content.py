import secrets
import time


class ContentStore:
    def create_content_item(self, filename, content_type, size, object_name, media_url):
        content_id = secrets.token_hex(16)
        created_at = int(time.time())
        with self.connection() as db:
            db.execute(
                """
                INSERT INTO content_items(id, filename, content_type, size, object_name, media_url, created_at, workspace_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (content_id, filename, content_type, size, object_name, media_url, created_at,
                 self.workspace.get()),
            )
        return self.get_content_item(content_id)

    def get_content_item(self, content_id):
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM content_items WHERE id=? AND workspace_id=?",
                (content_id, self.workspace.get()),
            ).fetchone()
        return dict(row) if row else None

    def list_content_items(self):
        with self.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM content_items WHERE workspace_id=? ORDER BY created_at DESC, id DESC",
                    (self.workspace.get(),),
                )
            ]
