from __future__ import annotations

import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from relay.schemas import Provider
from relay.store import digest

from .context import RouteContext


def register_oauth_routes(app, context: RouteContext):
    settings = context.settings
    store = context.store
    frontend = context.frontend
    protected = context.protected

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
