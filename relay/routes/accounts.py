from __future__ import annotations

from datetime import date, timedelta

from fastapi import HTTPException, Response

from .context import RouteContext


def register_accounts_routes(app, context: RouteContext):
    store = context.store
    protected = context.protected
    valid_account = context.valid_account

    @app.get("/accounts", dependencies=protected, tags=["Accounts"])
    async def accounts(include_avatars: bool = False):
        rows = store.list_accounts()
        if include_avatars:
            for index, row in enumerate(rows):
                if row.get("avatar_url"):
                    continue
                try:
                    account = await valid_account(row["id"], row["provider"])
                    avatar_url = await app.state.providers.account_avatar(
                        account["provider"],
                        account["external_id"],
                        store.decrypt(account["access_token"]),
                    )
                except HTTPException:
                    # A missing avatar should never prevent account selection.
                    continue
                if avatar_url:
                    rows[index] = store.update_account_avatar(row["id"], avatar_url)
        return {"accounts": [store.public_account(row) for row in rows]}

    @app.get("/analytics", dependencies=protected, tags=["Analytics"])
    async def analytics(days: int = 30):
        """Get provider performance for connected Instagram and YouTube accounts."""
        if days not in {7, 30, 90}:
            raise HTTPException(422, "Choose a reporting period of 7, 30, or 90 days.")
        end = date.today()
        start = end - timedelta(days=days - 1)
        period = {"days": days, "start_date": start.isoformat(), "end_date": end.isoformat()}
        results = []
        for account in store.list_accounts():
            item = {
                "account_id": account["id"],
                "provider": account["provider"],
                "name": account["name"],
                "status": "ready",
            }
            try:
                account = await valid_account(account["id"], account["provider"])
                token = store.decrypt(account["access_token"])
                if account["provider"] == "youtube":
                    item.update(
                        await app.state.providers.youtube_analytics(
                            account["external_id"], token, period["start_date"], period["end_date"]
                        )
                    )
                else:
                    posts = await app.state.providers.instagram_media(
                        account["external_id"], token, period["start_date"]
                    )
                    likes = sum(post["likes"] for post in posts)
                    comments = sum(post["comments"] for post in posts)
                    item["period"] = {
                        "posts": len(posts),
                        "likes": likes,
                        "comments": comments,
                        "average_interactions": round((likes + comments) / len(posts), 1)
                        if posts
                        else 0,
                    }
                    item["top_posts"] = sorted(
                        posts,
                        key=lambda post: (post["likes"] + post["comments"], post["published_at"]),
                        reverse=True,
                    )[:5]
            except HTTPException:
                item["status"] = "unavailable"
                item["message"] = (
                    "Analytics could not be loaded. Reconnect this channel or try again later."
                )
            results.append(item)
        return {"period": period, "accounts": results}

    @app.get("/posts", dependencies=protected, tags=["Publishing"])
    async def post_history():
        """List the latest posts published through Relay across connected channels."""
        posts = store.list_post_history()
        return {"posts": posts, "count": len(posts)}

    @app.post("/accounts/{account_id}/refresh", dependencies=protected, tags=["Accounts"])
    async def refresh(account_id: str):
        """Refresh stored tokens. Instagram requires a valid token at least 24 hours old."""
        return {"account": store.public_account(await valid_account(account_id, force=True))}

    @app.delete(
        "/accounts/{account_id}", dependencies=protected, tags=["Accounts"], status_code=204
    )
    async def disconnect(account_id: str):
        """Remove local tokens and Instagram job records. Does not revoke provider consent."""
        if not store.delete_account(account_id):
            raise HTTPException(404, "Connected account not found.")
        return Response(status_code=204)
