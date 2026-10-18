from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse

from relay.identity import SESSION_SECONDS
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

    @app.post("/auth/signup", tags=["Account"], status_code=201)
    async def signup(body: SignupRequest, request: Request, response: Response):
        same_origin(request)
        identity.throttle(request.client.host if request.client else "unknown", body.email)
        user_id, workspace_id = identity.signup(
            body.email, body.password, body.name, body.workspace_name
        )
        set_session(response, identity.new_session(
            user_id, workspace_id, request.cookies.get(session_cookie, "")
        ))
        return {"authenticated": True}

    @app.post("/auth/login", tags=["Account"])
    async def login(body: LoginRequest, request: Request, response: Response):
        same_origin(request)
        identity.throttle(request.client.host if request.client else "unknown", body.email)
        user_id, workspace_id = identity.login(body.email, body.password)
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
