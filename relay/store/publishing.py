import secrets
import time


class PublishingStore:
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
            row = db.execute(
                "SELECT p.* FROM instagram_posts p JOIN accounts a ON a.id=p.account_id "
                "WHERE p.id=? AND a.workspace_id=?", (post_id, self.workspace.get()),
            ).fetchone()
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
