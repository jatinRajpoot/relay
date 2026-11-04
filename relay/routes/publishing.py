"""Publishing recovery reads.

These endpoints are deliberately read-only with respect to external services:
listing or reading an attempt may age a stale claim in the database, but it
never calls a provider, so a status check can never re-send a publish.
"""

from __future__ import annotations

from typing import Literal

from fastapi import HTTPException

from relay.store.attempts import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor

from .context import RouteContext


def register_publishing_routes(app, context: RouteContext):
    store = context.store
    protected = context.protected

    @app.get("/publishing/attempts", dependencies=protected, tags=["Publishing"])
    async def list_attempts(
        status: Literal["active", "all"] = "active",
        limit: int = DEFAULT_LIMIT,
        cursor: str | None = None,
    ):
        """List this workspace's publishing receipts, newest first.

        ``active`` covers work that is in flight plus outcomes that still need a
        human decision (failed/unknown). ``all`` adds confirmed publishes and
        expired containers.
        """
        if limit < 1 or limit > MAX_LIMIT:
            raise HTTPException(422, f"limit must be between 1 and {MAX_LIMIT}.")
        if cursor:
            try:
                decode_cursor(cursor)
            except ValueError:
                raise HTTPException(422, "Invalid cursor.") from None
        # Age out unconfirmed claims before reading. Database-only: no provider call.
        store.reconcile_stale_attempts()
        rows, next_cursor = store.list_attempts(status=status, limit=limit, cursor=cursor)
        return {
            "attempts": [store.public_attempt(row) for row in rows],
            "count": len(rows),
            "next_cursor": next_cursor,
        }

    @app.get("/publishing/attempts/{attempt_id}", dependencies=protected, tags=["Publishing"])
    async def read_attempt(attempt_id: str):
        """Read one receipt. A status read never dispatches a provider call."""
        store.reconcile_stale_attempts()
        attempt = store.get_attempt(attempt_id)
        if not attempt:
            raise HTTPException(404, "Publishing attempt not found.")
        return store.public_attempt(attempt)
