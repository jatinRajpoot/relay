from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse

from relay.identity import SESSION_SECONDS, AuthWorkGate
from relay.schemas import LoginRequest, SignupRequest, UnlockRequest, WorkspaceRequest

from .context import RouteContext


def register_identity_routes(app, context: RouteContext):
    settings = context.settings
    store = context.store
    identity = context.identity
    frontend = context.frontend
    session_cookie = context.session_cookie
    session_marker = context.session_marker
    protected = context.protected
    legacy_session = context.legacy_session
    user_session = context.user_session
    bootstrap_session = context.bootstrap_session
    has_session = context.has_session
    same_origin = context.same_origin

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

    def client_address(request):
        return request.client.host if request.client else "unknown"

    # Per app instance, per event loop. Bounds concurrent scrypt work so an unauthenticated
    # flood cannot occupy every worker thread or stall the event loop. Published on app.state
    # so an operator or a test can inspect or replace it without reaching into the closure.
    app.state.auth_gate = AuthWorkGate()

    @app.post("/auth/signup", tags=["Account"], status_code=201)
    async def signup(body: SignupRequest, request: Request, response: Response):
        same_origin(request)
        # The complete account operation — password hash, duplicate check, row inserts
        # and the first session — runs in a bounded worker. On a cloud-backed store every
        # ``store.connection()`` is a snapshot download, so doing any of it on the event
        # loop would stall every other request. The gate, not a pre-check, bounds the
        # cost of a signup flood. A first signup is not an authentication attempt against
        # an existing account, so it neither consults nor spends the login-failure budget.
        #
        # The slot is held until the worker itself finishes, so a client that hangs up
        # mid-hash cannot free capacity for another expensive hash.
        token = await app.state.auth_gate.run(
            identity.signup_session,
            body.email, body.password, body.name, body.workspace_name,
            request.cookies.get(session_cookie, ""),
        )
        set_session(response, token)
        return {"authenticated": True}

    @app.post("/auth/login", tags=["Account"])
    async def login(body: LoginRequest, request: Request, response: Response):
        same_origin(request)
        address = client_address(request)
        # There is deliberately no pre-gate throttle check. It was a synchronous store
        # round trip on the event loop, and ``Identity.login`` re-checks the budget in
        # the worker immediately before hashing anyway. A rejected credential is recorded
        # by ``Identity.login`` itself, inside the worker, so cancelling the request
        # cannot dodge the throttle. Only a 503 from the gate is uncharged, and it did no
        # password work. The session insert also runs in that worker: it is another
        # store round trip, and only cookie construction is left on the loop.
        token = await app.state.auth_gate.run(
            identity.login_session, body.email, body.password, address,
            request.cookies.get(session_cookie, ""),
        )
        set_session(response, token)
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
        # Strict: a pin is persisted here, so a foreign or deleted id must be refused
        # rather than silently replaced with a fallback workspace.
        identity.select_workspace(request.cookies.get(session_cookie, ""), workspace_id)
        return {"workspace_id": workspace_id}

    @app.get("/session", tags=["Workspace"])
    async def session(request: Request):
        try:
            user = bootstrap_session(request)
        except HTTPException:
            try:
                # A pin naming another user's workspace is tampering: keep the 404.
                identity.session(request.cookies.get(session_cookie, ""))
            except HTTPException:
                # Signed in, but no workspace is left to resolve. Report an unauthenticated
                # shell (same shape as below) instead of a 404 so the client can send the
                # user back through sign-in rather than retrying a request that cannot work.
                user = None
            else:
                raise
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
                "content_storage_configured": bool(
                    settings.instagram_media_bucket or settings.local_media_only
                ),
                "local_media_storage": settings.local_media_only,
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
