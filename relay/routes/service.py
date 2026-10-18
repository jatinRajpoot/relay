from __future__ import annotations

from fastapi import Request
from fastapi.responses import FileResponse, RedirectResponse

from .context import RouteContext


def register_service_routes(app, context: RouteContext):
    frontend = context.frontend
    has_session = context.has_session

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

    @app.get("/try", include_in_schema=False)
    async def preview():
        return FileResponse(frontend / "try.html")
