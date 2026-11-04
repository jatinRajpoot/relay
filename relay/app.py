import asyncio
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import InvalidToken
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from relay.config import Settings
from relay.identity import Identity
from relay.providers import Providers
from relay.routes import (
    register_accounts_routes,
    register_content_routes,
    register_identity_routes,
    register_instagram_routes,
    register_oauth_routes,
    register_publishing_routes,
    register_service_routes,
    register_youtube_routes,
)
from relay.routes.context import CredentialsUnreadable, RouteContext
from relay.schemas import (
    InstagramPost,
    LoginRequest,
    Provider,
    SignupRequest,
    UnlockRequest,
    WorkspaceRequest,
)
from relay.store import StorageError, Store, digest
from relay.store.attempts import CONTAINER_FAILED_ERROR

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

# ── Delivery policy ───────────────────────────────────────────
# Only these suffixes are treated as public, cacheable frontend assets. The list is an
# allowlist on purpose: an HTML file (or anything unrecognised) under /static keeps the
# private no-store policy, so a future page can never inherit an asset cache directive.
PUBLIC_ASSET_SUFFIXES = frozenset(
    {".css", ".js", ".mjs", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif",
     ".ico", ".woff", ".woff2", ".ttf", ".map", ".txt"}
)
# Assets always revalidate: the shell HTML is no-store, so a deploy is visible on the
# next navigation and a revalidation costs a header-only 304. No immutable/one-year
# caching for unversioned paths.
PUBLIC_ASSET_CACHE = "public, max-age=0, must-revalidate"

# Suffixes whose bytes can change with Accept-Encoding. These get Vary even when a
# particular response was not compressed (a short file, or a 304 with no body), so a
# shared cache can never hand a gzip body to a client that did not ask for one.
COMPRESSIBLE_ASSET_SUFFIXES = frozenset({".css", ".js", ".mjs", ".svg", ".txt", ".map"})

# The application policy. Every script and stylesheet is a same-origin file; there are
# no inline scripts, inline styles, inline event handlers, or external code origins.
# Images may come from provider avatars and the public media bucket (https) plus the
# data: URI used by the stylesheet background; media is the same https media bucket.
# A broad https: image source is deliberate: guessing provider avatar hosts breaks
# profile images, and images are not script execution.
APP_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data: https:; "
    "media-src 'self' https:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

# FastAPI's bundled /docs, /redoc and /docs/oauth2-redirect pages load Swagger UI and
# Redoc from cdn.jsdelivr.net and build themselves with inline <script>/<style>. They are
# a deliberate, path-scoped exception: the rest of the app never gets 'unsafe-inline' or
# a CDN script origin. Self-hosting the docs bundles would mean a new dependency and
# build step, which is out of scope here.
#
# ReDoc additionally pulls its Google-hosted webfonts and starts a web worker from a
# blob: URL. Those are named explicitly rather than widening script-src, and they are
# scoped to the docs paths only.
DOCS_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
    "font-src 'self' data: https://cdn.jsdelivr.net https://fonts.gstatic.com; "
    "img-src 'self' data: https:; "
    "worker-src 'self' blob:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

PERMISSIONS_POLICY = "camera=(), geolocation=(), microphone=(), payment=(), usb=()"


def is_docs_path(path: str) -> bool:
    return path == "/redoc" or path == "/docs" or path.startswith("/docs/")


def is_public_asset(request: Request, response) -> bool:
    """True only for a successful, cookie-free frontend asset under /static/."""
    if request.method not in {"GET", "HEAD"}:
        return False
    path = request.url.path
    if not path.startswith("/static/"):
        return False
    if PurePosixPath(path).suffix.lower() not in PUBLIC_ASSET_SUFFIXES:
        return False
    if response.status_code not in {200, 304}:
        return False
    # A response that sets a cookie is private regardless of its path or suffix.
    return "set-cookie" not in response.headers


def add_vary(headers, value: str) -> None:
    """Append a Vary value without duplicating one already present.

    ``headers`` is a Starlette ``MutableHeaders`` in practice; it is case-insensitive
    there, but a plain mapping (used by the unit test) is not, so the existing value is
    read through a case-insensitive lookup and written back to the key that held it.
    """
    existing_key = next((key for key in headers if key.lower() == "vary"), None)
    existing = headers.get(existing_key) if existing_key else None
    if not existing:
        headers[existing_key or "Vary"] = value
        return
    values = [part.strip().lower() for part in existing.split(",")]
    if value.lower() not in values:
        headers[existing_key] = f"{existing}, {value}"


def create_app(settings: Settings | None = None, transport=None):
    settings = settings or Settings.from_env()
    # Constructing the store is also the cloud fail-closed check: with DATABASE_BUCKET
    # configured, a missing, empty, or corrupt snapshot raises here rather than being
    # replaced with an empty database. There is no runtime initialization flag.
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
    # Compress text payloads (CSS/JS/HTML/JSON) with the framework's own middleware.
    # Starlette already skips images, video, audio, fonts, archives and 206 range
    # responses, and adds Vary: Accept-Encoding itself. Added before the security
    # middleware so the latter's headers are applied to the compressed response.
    app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=6)
    app.state.store = store
    app.state.refresh_locks = refresh_locks

    @app.exception_handler(StorageError)
    async def storage_failure(request: Request, error: StorageError):
        """A storage failure is a sanitised 503, never a raw Google/SQLite error.

        ``outcome`` tells the client whether the change is known not to have been
        applied (safe to retry) or may have been (verify first). It describes the
        storage write only, not any provider operation the request also made.
        """
        return JSONResponse(
            {"detail": error.detail, "outcome": error.outcome}, status_code=503
        )

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

    def bootstrap_session(request):
        # The shell and /session must survive a pin left over from a deleted workspace.
        # API routes keep strict pin enforcement through user_session.
        return identity.session(
            request.cookies.get(session_cookie, ""),
            request.headers.get("x-workspace-id"),
            strict=False,
        )

    def has_session(request):
        if legacy_session(request):
            return True
        try:
            return bool(bootstrap_session(request))
        except HTTPException:
            # Authenticated but with no workspace left: treat as no usable session so the
            # shell redirects to sign-in instead of returning a 404 page.
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
        public_asset = is_public_asset(request, response)
        # Everything that can carry session state, account data, or an HTML shell stays
        # no-store. Only genuinely public frontend assets get a revalidating cache policy.
        response.headers["Cache-Control"] = (
            PUBLIC_ASSET_CACHE if public_asset else "no-store"
        )
        if public_asset and PurePosixPath(request.url.path).suffix.lower() in (
            COMPRESSIBLE_ASSET_SUFFIXES
        ):
            # A 304 carries no body, so GZipMiddleware cannot add Vary to it. Set it
            # here for every compressible asset so a shared cache never mixes the
            # gzip and identity representations of the same URL.
            add_vary(response.headers, "Accept-Encoding")
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = PERMISSIONS_POLICY
        # HSTS is only meaningful (and only safe) on an HTTPS deployment. Local HTTP
        # development must keep working, and includeSubDomains is omitted because this
        # app does not own its parent domain.
        if settings.base_url.startswith("https://"):
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        # No Cross-Origin-Opener-Policy: the OAuth completion page reports back through
        # window.opener.postMessage, which a same-origin COOP would sever.
        response.headers["Content-Security-Policy"] = (
            DOCS_CSP if is_docs_path(request.url.path) else APP_CSP
        )
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

    def account_token(account, field="access_token"):
        """Decrypt a stored credential, isolating an unreadable row to its own account.

        A rotated TOKEN_ENCRYPTION_KEY, or a database restored under a different key,
        makes stored tokens undecryptable. That must disable only the affected channel
        (reconnect it) instead of failing every request that touches the workspace.

        ``CredentialsUnreadable`` is a distinct type so callers can record the one
        correct remedy (reconnect) instead of offering a retry that would fail the
        same way forever.
        """
        try:
            return store.decrypt(account[field])
        except InvalidToken:
            raise CredentialsUnreadable() from None

    async def valid_account(account_id, provider=None, force=False):
        # Validate before allocating a lock: otherwise any unknown id would leave a
        # permanent entry in refresh_locks. The lookup is repeated under the lock.
        account_or_404(account_id, provider)
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
                    account_token(account),
                    account_token(account, "refresh_token"),
                )
                # The account may have been disconnected while the provider call was
                # in flight. update_tokens then matches no row and returns None, so
                # re-validate instead of handing None to public_account (a 500).
                account = store.update_tokens(account_id, tokens)
                if account is None:
                    raise HTTPException(404, "Connected account not found.")
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
            account_token(account),
        )
        if post["status"] == "publishing":
            if remote == "PUBLISHED":
                return store.sync_post_status(post["id"], "publishing", "published")
            # A claim that was taken and never answered is uncertain, not retryable.
            # This is a database transition only: the status read above never
            # re-sends the publish.
            if store.attempt_claim_is_stale(post["id"]):
                return store.sync_post_status(post["id"], "publishing", "unknown")
            return post
        status = {
            "FINISHED": "ready",
            "IN_PROGRESS": "processing",
            "ERROR": "failed",
            "EXPIRED": "expired",
            "PUBLISHED": "published",
        }[remote]
        # A timed-out publish may still complete. Never automatically retry an uncertain
        # publish, and never let a merely-ready/processing container look like permission
        # to send again. A remote PUBLISHED is definitive; a container that ERRORed or
        # EXPIRED was never published, which is also definitive.
        if post["status"] == "unknown" and status in {"ready", "processing"}:
            return post
        if status == "failed":
            # The media never became a publishable container. Record that provenance
            # explicitly: a bare ``failed`` job would otherwise be indistinguishable
            # from a refused publish, and the retry recipe would re-arm a container
            # the provider will reject forever. The message is what the user needs.
            return store.sync_post_status(
                post["id"],
                post["status"],
                status,
                failure_stage="container",
                error=CONTAINER_FAILED_ERROR,
            )
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
        bootstrap_session=bootstrap_session,
        has_session=has_session,
        same_origin=same_origin,
        valid_account=valid_account,
        account_token=account_token,
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
    register_publishing_routes(app, context)

    return app
