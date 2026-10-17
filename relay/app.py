import asyncio
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import InvalidToken
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl, field_validator

from relay.config import Settings
from relay.providers import Providers
from relay.store import Store, digest

Provider = Literal["youtube", "instagram"]
ApiKey = APIKeyHeader(name="X-API-Key", auto_error=False)


class InstagramPost(BaseModel):
    account_id: str = Field(min_length=1, max_length=64)
    media_type: Literal["IMAGE", "REELS"]
    media_url: HttpUrl
    caption: str = Field(default="", max_length=2200)

    @field_validator("media_url")
    @classmethod
    def public_https_url(cls, value):
        if value.scheme != "https" or value.username or value.password or value.fragment:
            raise ValueError("Use a publicly accessible HTTPS media URL without credentials.")
        return value


class UnlockRequest(BaseModel):
    service_key: str = Field(min_length=1, max_length=1024, repr=False)


def create_app(settings: Settings | None = None, transport=None):
    settings = settings or Settings.from_env()
    store = Store(settings.database_path, settings.encryption_key)
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
        description="Single-operator Instagram and YouTube account connections and publishing. "
        "Authorize protected operations with the SERVICE_API_KEY from .env.",
    )
    app.state.store = store
    frontend = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=frontend), name="static")
    session_cookie = "relay_session"
    session_marker = f"relay-browser:{digest(settings.api_key)}".encode()

    def has_session(request):
        try:
            token = request.cookies.get(session_cookie, "").encode()
            return secrets.compare_digest(store.cipher.decrypt(token, ttl=8 * 3600), session_marker)
        except (InvalidToken, ValueError, TypeError):
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

    def require_key(request: Request, key: Annotated[str | None, Depends(ApiKey)]):
        if key and secrets.compare_digest(key.encode(), settings.api_key.encode()):
            return
        if has_session(request):
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                same_origin(request)
            return
        raise HTTPException(401, "Unlock your workspace or supply a valid X-API-Key.")

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
        if post["status"] in {"published", "failed", "expired", "publishing"}:
            return post
        account = await valid_account(post["account_id"], "instagram")
        remote = await app.state.providers.instagram_status(
            post["container_id"],
            store.decrypt(account["access_token"]),
        )
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
        with store.connection() as db:
            db.execute(
                """
                UPDATE instagram_posts SET status=? WHERE id=? AND status=?
            """,
                (status, post["id"], post["status"]),
            )
        return store.get_post(post["id"])

    @app.get("/health", tags=["Service"])
    async def health():
        return {"status": "ok"}

    @app.get("/", tags=["Service"])
    async def index():
        return FileResponse(frontend / "index.html")

    @app.get("/session", tags=["Workspace"])
    async def session(request: Request):
        return {
            "authenticated": has_session(request),
            "providers": {
                provider: {
                    "configured": all(settings.credentials(provider)),
                    "redirect_uri": settings.redirect_uri(provider),
                    "setup_issue": settings.oauth_setup_issue(provider),
                }
                for provider in ("instagram", "youtube")
            },
        }

    @app.post("/session", tags=["Workspace"])
    async def unlock(body: UnlockRequest, request: Request, response: Response):
        same_origin(request)
        if not secrets.compare_digest(body.service_key.encode(), settings.api_key.encode()):
            raise HTTPException(
                401, "That service key isn't correct. Check SERVICE_API_KEY in .env."
            )
        response.set_cookie(
            session_cookie,
            store.cipher.encrypt(session_marker).decode(),
            max_age=8 * 3600,
            httponly=True,
            secure=settings.base_url.startswith("https://"),
            samesite="lax",
            path="/",
        )
        return {"authenticated": True}

    @app.delete("/session", dependencies=protected, tags=["Workspace"], status_code=204)
    async def lock_workspace(response: Response):
        response.delete_cookie(session_cookie, path="/")
        response.status_code = 204
        return response

    @app.post("/oauth/{provider}/authorize", dependencies=protected, tags=["OAuth"])
    async def authorize(
        provider: Provider, response: Response, ui: bool = False, popup: bool = False
    ):
        """Open authorization_url in this SAME browser within 10 minutes (cookie binding)."""
        if ui and (issue := settings.oauth_setup_issue(provider)):
            raise HTTPException(409, issue)
        browser = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64) if provider == "youtube" else None
        state = store.create_state(provider, browser, verifier, return_to_ui=ui, popup=ui and popup)
        url = app.state.providers.authorization_url(provider, state, verifier)
        callback_path = urlsplit(settings.redirect_uri(provider)).path
        response.set_cookie(
            f"relay_oauth_{digest(state)[:16]}",
            browser,
            max_age=600,
            httponly=True,
            secure=settings.redirect_uri(provider).startswith("https://"),
            samesite="lax",
            path=callback_path,
        )
        return {"authorization_url": url, "expires_in": 600}

    @app.get("/oauth/wait", include_in_schema=False)
    @app.get("/oauth/complete", include_in_schema=False)
    async def popup_page():
        return FileResponse(frontend / "oauth.html")

    @app.get("/oauth/{provider}/callback", tags=["OAuth"])
    async def callback(
        provider: Provider,
        request: Request,
        state: str = "",
        code: str = "",
        error: str = "",
    ):
        cookie_name = f"relay_oauth_{digest(state)[:16]}"
        browser = request.cookies.get(cookie_name, "")
        saved = store.consume_state(state, provider, browser) if state and browser else None
        if saved is None:
            if "text/html" in request.headers.get("accept", ""):
                # Completion can also notify a popup's opener when state expired or cookies were lost.
                result = RedirectResponse(
                    f"/oauth/complete?provider={provider}&result=expired", status_code=303
                )
                result.delete_cookie(
                    cookie_name, path=urlsplit(settings.redirect_uri(provider)).path
                )
                return result
            raise HTTPException(
                400, "Invalid, expired, or reused OAuth state. Start again in this browser."
            )
        try:
            if error:
                raise HTTPException(
                    400, "Authorization was denied or cancelled. Start again to connect."
                )
            if not code:
                raise HTTPException(400, "Missing authorization code. Start again.")
            tokens, external_id, name, scopes = await app.state.providers.exchange(
                provider,
                code,
                saved["verifier"],
            )
            account = store.save_account(provider, external_id, name, tokens, scopes)
            result = (
                RedirectResponse(
                    f"/oauth/complete?provider={provider}&result=connected"
                    if saved["popup"]
                    else f"/?connected={provider}",
                    status_code=303,
                )
                if saved["return_to_ui"]
                else JSONResponse(
                    {"message": "Account connected.", "account": store.public_account(account)}
                )
            )
        except HTTPException:
            if not saved["return_to_ui"]:
                raise
            reason = "cancelled" if error else "connection"
            result = RedirectResponse(
                f"/oauth/complete?provider={provider}&result={reason}"
                if saved["popup"]
                else f"/?oauth_error={reason}",
                status_code=303,
            )
        result.delete_cookie(cookie_name, path=urlsplit(settings.redirect_uri(provider)).path)
        return result

    @app.get("/accounts", dependencies=protected, tags=["Accounts"])
    async def accounts():
        return {"accounts": [store.public_account(row) for row in store.list_accounts()]}

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
        return store.create_post(account["id"], container_id)

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

    @app.post("/youtube/videos", dependencies=protected, tags=["YouTube"], status_code=201)
    async def upload_video(
        account_id: Annotated[str, Form(min_length=1, max_length=64)],
        title: Annotated[str, Form(min_length=1, max_length=100)],
        file: Annotated[UploadFile, File()],
        description: Annotated[str, Form(max_length=5000)] = "",
        privacy_status: Annotated[Literal["private", "unlisted", "public"], Form()] = "private",
        category_id: Annotated[str, Form(pattern=r"^\d+$")] = "22",
        made_for_kids: Annotated[bool, Form()] = False,
    ):
        """Upload a video using YouTube's resumable session protocol. Defaults to private."""
        try:
            if not title.strip() or "<" in title or ">" in title:
                raise HTTPException(422, "Title must contain text and cannot include < or >.")
            if not file.size or file.size > settings.max_upload_bytes:
                raise HTTPException(413, "Video is empty or exceeds MAX_UPLOAD_BYTES.")
            mime = file.content_type or "application/octet-stream"
            if not (mime.startswith("video/") or mime == "application/octet-stream"):
                raise HTTPException(
                    422, "Upload a video file (video/* or application/octet-stream)."
                )
            account = await valid_account(account_id, "youtube")
            metadata = {
                "snippet": {"title": title, "description": description, "categoryId": category_id},
                "status": {
                    "privacyStatus": privacy_status,
                    "selfDeclaredMadeForKids": made_for_kids,
                },
            }
            return await app.state.providers.upload_youtube(
                store.decrypt(account["access_token"]),
                file,
                metadata,
            )
        finally:
            await file.close()

    return app
