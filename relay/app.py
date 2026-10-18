import asyncio
import mimetypes
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import Annotated, Literal
from urllib.parse import quote, urlsplit

import httpx
from cryptography.fernet import InvalidToken
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import GoogleAuthError
from pydantic import BaseModel, Field, HttpUrl, field_validator
from starlette.datastructures import Headers

from relay.config import Settings
from relay.identity import SESSION_SECONDS, Identity
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


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128, repr=False)

    @field_validator("email")
    @classmethod
    def normalized_email(cls, value):
        value = value.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Enter a valid email address.")
        return value


class WorkspaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def trimmed_name(cls, value):
        if not value.strip():
            raise ValueError("Enter a name.")
        return value.strip()


class SignupRequest(LoginRequest):
    password: str = Field(min_length=12, max_length=128, repr=False)
    name: str = Field(min_length=1, max_length=80)
    workspace_name: str = Field(default="My workspace", min_length=1, max_length=80)

    @field_validator("name", "workspace_name")
    @classmethod
    def trimmed_name(cls, value):
        return WorkspaceRequest.trimmed_name(value)


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
        return bool(user_session(request) or legacy_session(request))

    def same_origin(request):
        expected = urlsplit(settings.base_url)
        if request.headers.get("origin") != f"{expected.scheme}://{expected.netloc}":
            raise HTTPException(403, "Open this workspace at its configured BASE_URL to continue.")

    def client_address(request):
        return request.client.host if request.client else "unknown"

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

    @app.get("/health", tags=["Service"])
    async def health():
        return {"status": "ok"}

    @app.get("/", tags=["Service"])
    async def index(request: Request):
        return FileResponse(frontend / ("index.html" if has_session(request) else "landing.html"))

    @app.get("/app", include_in_schema=False)
    async def dashboard(request: Request):
        if not has_session(request):
            return RedirectResponse("/login", status_code=303)
        return FileResponse(frontend / "index.html")

    @app.get("/login", include_in_schema=False)
    @app.get("/signup", include_in_schema=False)
    async def auth_page(request: Request):
        if has_session(request):
            return RedirectResponse("/app", status_code=303)
        return FileResponse(frontend / "auth.html")

    def set_session(response, token):
        response.set_cookie(
            session_cookie, token, max_age=SESSION_SECONDS, httponly=True,
            secure=settings.base_url.startswith("https://"), samesite="lax", path="/",
        )

    @app.post("/auth/signup", tags=["Account"], status_code=201)
    async def signup(body: SignupRequest, request: Request, response: Response):
        same_origin(request)
        address = client_address(request)
        identity.throttle(address, body.email)
        try:
            user_id, workspace_id = identity.signup(
                body.email, body.password, body.name, body.workspace_name
            )
        except HTTPException:
            identity.record_failure(address, body.email)
            raise
        set_session(response, identity.new_session(
            user_id, workspace_id, request.cookies.get(session_cookie, "")
        ))
        return {"authenticated": True}

    @app.post("/auth/login", tags=["Account"])
    async def login(body: LoginRequest, request: Request, response: Response):
        same_origin(request)
        address = client_address(request)
        identity.throttle(address, body.email)
        try:
            user_id, workspace_id = identity.login(body.email, body.password)
        except HTTPException:
            identity.record_failure(address, body.email)
            raise
        set_session(response, identity.new_session(
            user_id, workspace_id, request.cookies.get(session_cookie, "")
        ))
        return {"authenticated": True}

    def require_user(request):
        user = user_session(request)
        if not user:
            raise HTTPException(401, "Log in with your email to manage workspaces.")
        return user

    @app.get("/workspaces", dependencies=protected, tags=["Workspace"])
    async def workspaces(request: Request):
        return {"workspaces": identity.workspaces(require_user(request)["id"])}

    @app.post("/workspaces", dependencies=protected, tags=["Workspace"], status_code=201)
    async def create_workspace(body: WorkspaceRequest, request: Request):
        return identity.create_workspace(require_user(request)["id"], body.name)

    @app.post("/workspaces/{workspace_id}/select", dependencies=protected, tags=["Workspace"])
    async def select_workspace(workspace_id: str, request: Request):
        require_user(request)
        identity.select_workspace(request.cookies.get(session_cookie, ""), workspace_id)
        return {"workspace_id": workspace_id}

    @app.get("/session", tags=["Workspace"])
    async def session(request: Request):
        user = user_session(request)
        return {
            "authenticated": bool(user or legacy_session(request)),
            "user": {key: user[key] for key in ("id", "name", "email")} if user else None,
            "workspace": user["workspace"] if user else None,
            "workspaces": identity.workspaces(user["id"]) if user else [],
            "providers": {
                provider: {
                    "configured": all(settings.credentials(provider)),
                    "redirect_uri": settings.redirect_uri(provider),
                    "setup_issue": settings.oauth_setup_issue(provider),
                }
                for provider in ("instagram", "youtube")
            },
            "publishing": {
                "instagram_media_configured": bool(settings.instagram_media_bucket),
                "content_storage_configured": bool(settings.instagram_media_bucket),
                "max_upload_bytes": settings.max_upload_bytes,
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
    async def lock_workspace(request: Request, response: Response):
        identity.logout(request.cookies.get(session_cookie, ""))
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
        store.workspace.set(saved.get("workspace_id", "legacy"))
        try:
            if error:
                raise HTTPException(
                    400, "Authorization was denied or cancelled. Start again to connect."
                )
            if not code:
                raise HTTPException(400, "Missing authorization code. Start again.")
            tokens, external_id, name, scopes, avatar_url = await app.state.providers.exchange(
                provider,
                code,
                saved["verifier"],
            )
            account = store.save_account(provider, external_id, name, tokens, scopes, avatar_url)
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

    async def save_content_file(file: UploadFile):
        if not settings.instagram_media_bucket:
            raise HTTPException(503, "Instagram media storage is not configured on this service.")
        media_type = (file.content_type or "").lower()
        if (
            media_type != "image/jpeg"
            and not media_type.startswith("video/")
            and media_type != "application/octet-stream"
        ):
            raise HTTPException(422, "Choose a JPEG image or a video file.")
        if not file.size or file.size > settings.max_upload_bytes:
            raise HTTPException(413, "Media is empty or exceeds MAX_UPLOAD_BYTES.")
        extension = mimetypes.guess_extension(media_type) or ".bin"
        if extension == ".jpe":
            extension = ".jpg"
        object_name = f"content/{secrets.token_urlsafe(24)}{extension}"
        filename = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
        filename = "".join(character for character in filename if character.isprintable())[:255]
        filename = filename or f"Untitled{extension}"
        try:
            from google.cloud import storage

            blob = storage.Client().bucket(settings.instagram_media_bucket).blob(object_name)
            blob.upload_from_file(
                file.file,
                rewind=True,
                size=file.size,
                content_type=media_type,
                if_generation_match=0,
            )
        except (GoogleAPIError, GoogleAuthError, OSError, ValueError):
            raise HTTPException(502, "Could not upload media to Google Cloud Storage.") from None
        media_url = (
            f"https://storage.googleapis.com/{quote(settings.instagram_media_bucket, safe='')}/"
            f"{quote(object_name, safe='/')}"
        )
        try:
            item = store.create_content_item(
                filename, media_type, file.size, object_name, media_url
            )
        except Exception:
            try:
                blob.delete()
            except Exception:
                pass
            raise HTTPException(
                502, "Could not save the uploaded media to your content library."
            ) from None
        return item

    def public_content_item(item):
        return {
            "id": item["id"],
            "filename": item["filename"],
            "content_type": item["content_type"],
            "size": item["size"],
            "media_url": item["media_url"],
            "created_at": item["created_at"],
            "instagram_compatible": item["content_type"] in {"image/jpeg", "video/mp4"},
            "youtube_compatible": item["content_type"].startswith("video/")
            or item["content_type"] == "application/octet-stream",
        }

    @app.get("/content", dependencies=protected, tags=["Content"])
    async def list_content():
        return {"items": [public_content_item(item) for item in store.list_content_items()]}

    @app.post("/content", dependencies=protected, tags=["Content"], status_code=201)
    async def upload_content(file: Annotated[UploadFile, File()]):
        """Save a JPEG or video file in the workspace media library."""
        try:
            item = await save_content_file(file)
            return public_content_item(item)
        finally:
            await file.close()

    @app.post("/instagram/media", dependencies=protected, tags=["Instagram"], status_code=201)
    async def upload_instagram_media(file: Annotated[UploadFile, File()]):
        """Upload JPEG or MP4 media to the configured public GCS bucket for Instagram to fetch."""
        try:
            if (file.content_type or "").lower() not in {"image/jpeg", "video/mp4"}:
                raise HTTPException(422, "Choose a JPEG image or an MP4 video for Instagram.")
            item = await save_content_file(file)
        finally:
            await file.close()
        return {
            "media_url": item["media_url"],
            "media_type": "IMAGE" if item["content_type"] == "image/jpeg" else "REELS",
            "content_id": item["id"],
        }

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

    @app.post("/youtube/videos", dependencies=protected, tags=["YouTube"], status_code=201)
    async def upload_video(
        account_id: Annotated[str, Form(min_length=1, max_length=64)],
        title: Annotated[str, Form(min_length=1, max_length=100)],
        file: Annotated[UploadFile | None, File()] = None,
        content_id: Annotated[str | None, Form()] = None,
        description: Annotated[str, Form(max_length=5000)] = "",
        privacy_status: Annotated[Literal["private", "unlisted", "public"], Form()] = "private",
        category_id: Annotated[str, Form(pattern=r"^\d+$")] = "22",
        made_for_kids: Annotated[bool, Form()] = False,
    ):
        """Upload a video using YouTube's resumable session protocol. Defaults to private."""
        if not title.strip() or "<" in title or ">" in title:
            raise HTTPException(422, "Title must contain text and cannot include < or >.")
        temporary_file = None
        content_item = None
        if content_id:
            if file:
                await file.close()
                raise HTTPException(422, "Choose a saved content item or upload a file, not both.")
            item = store.get_content_item(content_id)
            if not item:
                raise HTTPException(404, "Content item not found. Refresh your content library.")
            content_item = item
            if not (
                item["content_type"].startswith("video/")
                or item["content_type"] == "application/octet-stream"
            ):
                raise HTTPException(422, "Choose a video from your content library for YouTube.")
            if not item["size"] or item["size"] > settings.max_upload_bytes:
                raise HTTPException(413, "Video is empty or exceeds MAX_UPLOAD_BYTES.")
            if not settings.instagram_media_bucket:
                raise HTTPException(503, "Content storage is not configured on this service.")
            try:
                from google.cloud import storage

                temporary_file = SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
                storage.Client().bucket(settings.instagram_media_bucket).blob(
                    item["object_name"]
                ).download_to_file(temporary_file)
                temporary_file.seek(0)
                file = UploadFile(
                    filename=item["filename"],
                    file=temporary_file,
                    size=item["size"],
                    headers=Headers({"content-type": item["content_type"]}),
                )
            except (GoogleAPIError, GoogleAuthError, OSError, ValueError):
                if temporary_file is not None:
                    temporary_file.close()
                raise HTTPException(
                    502, "Could not read the selected content from storage."
                ) from None
        if file is None:
            raise HTTPException(422, "Choose a video from your content library.")
        try:
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
            uploaded = await app.state.providers.upload_youtube(
                store.decrypt(account["access_token"]),
                file,
                metadata,
            )
            store.save_youtube_history(
                account["id"],
                uploaded["id"],
                title.strip(),
                description,
                file.filename or "",
                mime,
                content_item["media_url"] if content_item else None,
                uploaded["url"],
                uploaded.get("privacy_status") or privacy_status,
            )
            return uploaded
        finally:
            await file.close()

    return app
