import asyncio
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import InvalidToken
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles

from relay.config import Settings
from relay.identity import Identity
from relay.providers import Providers
from relay.routes import (
    register_accounts_routes,
    register_content_routes,
    register_identity_routes,
    register_instagram_routes,
    register_oauth_routes,
    register_service_routes,
    register_youtube_routes,
)
from relay.routes.context import RouteContext
from relay.schemas import (
    InstagramPost,
    LoginRequest,
    Provider,
    SignupRequest,
    UnlockRequest,
    WorkspaceRequest,
)
from relay.store import Store, digest

__all__ = [
    "InstagramPost",
    "LoginRequest",
    "Provider",
    "SignupRequest",
    "UnlockRequest",
    "WorkspaceRequest",
    "create_app",
]

ApiKey = APIKeyHeader(name="X-API-Key", auto_error=False)


def create_app(settings: Settings | None = None, transport=None):
    settings = settings or Settings.from_env()
    store = Store(settings.database_path, settings.encryption_key)
    identity = Identity(store)
    refresh_locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def lifespan(app):
        # Disable environment proxy inheritance and automatic redirects for token requests.
        async with httpx.AsyncClient(
            timeout=30,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            app.state.providers = Providers(settings, client)
            yield

    app = FastAPI(
        title="Relay OAuth & Publishing",
        version="0.1.0",
        lifespan=lifespan,
        description="Account-based Instagram and YouTube publishing with private workspaces. "
        "The service API key accesses only the legacy workspace.",
    )
    app.state.store = store
    frontend = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=frontend), name="static")
    session_cookie = "relay_session"
    session_marker = f"relay-browser:{digest(settings.api_key)}".encode()

    def legacy_session(request):
        try:
            token = request.cookies.get(session_cookie, "").encode()
            return secrets.compare_digest(store.cipher.decrypt(token, ttl=8 * 3600), session_marker)
        except (InvalidToken, ValueError, TypeError):
            return False

    def user_session(request):
        return identity.session(
            request.cookies.get(session_cookie, ""), request.headers.get("x-workspace-id")
        )

    def has_session(request):
        token = request.cookies.get(session_cookie, "")
        try:
            if user_session(request) or legacy_session(request):
                return True
        except HTTPException:
            # A stale X-Workspace-ID pin must not 404 the HTML shell. Fall back to the
            # session's own workspace; API routes still reject the pin.
            try:
                return bool(identity.session(token))
            except HTTPException:
                return False
        return False

    def same_origin(request):
        expected = urlsplit(settings.base_url)
        if request.headers.get("origin") != f"{expected.scheme}://{expected.netloc}":
            raise HTTPException(403, "Open this workspace at its configured BASE_URL to continue.")

    @app.middleware("http")
    async def response_security(request: Request, call_next):
        # Reject declared oversized requests before multipart parsing/spooling.
        length = request.headers.get("content-length")
        if length:
            try:
                if int(length) > settings.max_upload_bytes + 1024 * 1024:
                    return JSONResponse(
                        {"detail": "Upload exceeds MAX_UPLOAD_BYTES."}, status_code=413
                    )
            except ValueError:
                return JSONResponse({"detail": "Invalid Content-Length."}, status_code=400)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    async def require_key(request: Request, key: Annotated[str | None, Depends(ApiKey)]):
        if key and secrets.compare_digest(key.encode(), settings.api_key.encode()):
            store.workspace.set("legacy")
            return
        user = user_session(request)
        if user or legacy_session(request):
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                same_origin(request)
            store.workspace.set(user["workspace_id"] if user else "legacy")
            return
        raise HTTPException(401, "Log in to continue.")

    protected = [Depends(require_key)]

    def account_or_404(account_id, provider=None):
        account = store.get_account(account_id)
        if account is None or (provider and account["provider"] != provider):
            raise HTTPException(404, "Connected account not found.")
        return account

    async def valid_account(account_id, provider=None, force=False):
        # Serialize refreshes per account and re-read after obtaining the lock.
        lock = refresh_locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            account = account_or_404(account_id, provider)
            now = int(time.time())
            threshold = 86400 if account["provider"] == "instagram" else 120
            if force or account["expires_at"] <= now + threshold:
                if account["provider"] == "instagram":
                    if account["expires_at"] <= now:
                        raise HTTPException(409, "Instagram token expired. Reconnect the account.")
                    if account["token_updated_at"] > now - 86400:
                        if force:
                            raise HTTPException(
                                409, "Instagram tokens can be refreshed after 24 hours."
                            )
                        return account
                tokens = await app.state.providers.refresh(
                    account["provider"],
                    store.decrypt(account["access_token"]),
                    store.decrypt(account["refresh_token"]),
                )
                account = store.update_tokens(account_id, tokens)
            return account

    def post_or_404(post_id):
        post = store.get_post(post_id)
        if not post:
            raise HTTPException(404, "Instagram post not found.")
        return post

    async def sync_post(post):
        if post["status"] in {"published", "failed", "expired"}:
            return post
        account = await valid_account(post["account_id"], "instagram")
        remote = await app.state.providers.instagram_status(
            post["container_id"],
            store.decrypt(account["access_token"]),
        )
        if post["status"] == "publishing" and remote != "PUBLISHED":
            return post
        status = {
            "FINISHED": "ready",
            "IN_PROGRESS": "processing",
            "ERROR": "failed",
            "EXPIRED": "expired",
            "PUBLISHED": "published",
        }[remote]
        # A timed-out publish may still complete. Never automatically retry an uncertain publish.
        if post["status"] == "unknown" and status in {"ready", "processing"}:
            return post
        # Do not overwrite a concurrent publish claim with a stale GET result.
        return store.sync_post_status(post["id"], post["status"], status)

    context = RouteContext(
        settings=settings,
        store=store,
        identity=identity,
        frontend=frontend,
        session_cookie=session_cookie,
        session_marker=session_marker,
        protected=protected,
        legacy_session=legacy_session,
        user_session=user_session,
        has_session=has_session,
        same_origin=same_origin,
        valid_account=valid_account,
        post_or_404=post_or_404,
        sync_post=sync_post,
    )
    register_service_routes(app, context)
    register_identity_routes(app, context)
    register_oauth_routes(app, context)
    register_accounts_routes(app, context)
    register_content_routes(app, context)
    register_instagram_routes(app, context)
    register_youtube_routes(app, context)

    return app
