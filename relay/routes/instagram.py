from __future__ import annotations

from fastapi import HTTPException

from relay.schemas import InstagramPost

from .context import RouteContext


def register_instagram_routes(app, context: RouteContext):
    store = context.store
    protected = context.protected
    valid_account = context.valid_account
    sync_post = context.sync_post
    post_or_404 = context.post_or_404

    @app.post("/instagram/posts", dependencies=protected, tags=["Instagram"], status_code=201)
    async def create_post(body: InstagramPost):
        """Create an unpublished image/Reel container. Check status, then call /publish."""
        account = await valid_account(body.account_id, "instagram")
        container_id = await app.state.providers.create_instagram_post(
            account["external_id"],
            store.decrypt(account["access_token"]),
            body.media_type,
            str(body.media_url),
            body.caption,
        )
        return store.create_post(
            account["id"],
            container_id,
            body.media_type,
            str(body.media_url),
            body.caption,
        )

    @app.get("/instagram/posts/{post_id}", dependencies=protected, tags=["Instagram"])
    async def post_status(post_id: str):
        """Poll at most once per minute until ready. Processing can take several minutes."""
        return await sync_post(post_or_404(post_id))

    @app.post("/instagram/posts/{post_id}/publish", dependencies=protected, tags=["Instagram"])
    async def publish(post_id: str):
        """Publish a ready container immediately. Repeated calls return the saved published result."""
        post = await sync_post(post_or_404(post_id))
        if post["status"] == "published":
            return post
        if post["status"] != "ready":
            raise HTTPException(
                409, f"Post is {post['status']}; only ready posts can be published."
            )
        account = await valid_account(post["account_id"], "instagram")
        if not store.claim_post(post_id):
            raise HTTPException(409, "Post is already being published. Check its status.")
        try:
            media_id = await app.state.providers.publish_instagram(
                account["external_id"],
                store.decrypt(account["access_token"]),
                post["container_id"],
            )
        except Exception:
            store.update_post(post_id, "unknown")
            raise
        return store.update_post(post_id, "published", media_id)
