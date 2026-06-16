"""FastAPI application factory for the Adminbot manager UI."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from adminbot.app.manager import AdminbotManager
from adminbot.app.paths import build_runtime_paths
from adminbot.app.web.auth import SESSION_COOKIE, AdminbotAuthStore, LoginRateLimiter, SessionStore

_WEB_DIR = Path(__file__).parent
_TEMPLATES_DIR = _WEB_DIR / "templates"
_STATIC_DIR = _WEB_DIR / "static"


def create_app() -> FastAPI:
    app = FastAPI(
        title="Adminbot Manager",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    paths = build_runtime_paths()
    manager = AdminbotManager(paths)
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    auth_store = AdminbotAuthStore(paths.runtime_root / "auth.json")
    auth_store.ensure_state()
    sessions = SessionStore()
    login_rate_limiter = LoginRateLimiter()

    app.state.paths = paths
    app.state.manager = manager
    app.state.templates = templates
    app.state.auth_store = auth_store
    app.state.sessions = sessions
    app.state.login_rate_limiter = login_rate_limiter

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        token = request.cookies.get(SESSION_COOKIE)
        authenticated = request.app.state.sessions.valid(token)
        default_password_active = request.app.state.auth_store.ensure_state().is_default_password
        request.state.authenticated = authenticated
        request.state.default_password_active = default_password_active

        public_paths = {"/login", "/change-password", "/logout"}
        if path.startswith("/static/") or path in public_paths:
            return await call_next(request)

        wants_json = path.startswith("/api/") or "application/json" in request.headers.get("accept", "")
        if not authenticated:
            if wants_json:
                return JSONResponse({"error": "Authentication required"}, status_code=401)
            return RedirectResponse("/login", status_code=303)
        if default_password_active:
            if wants_json:
                return JSONResponse({"error": "Change password before managing bots"}, status_code=403)
            return RedirectResponse(
                "/change-password?error=Change+password+before+managing+bots",
                status_code=303,
            )
        return await call_next(request)

    from adminbot.app.web.routes.auth import router as auth_router
    from adminbot.app.web.routes.bots import router as bots_router
    from adminbot.app.web.routes.dashboard import router as dashboard_router
    from adminbot.app.web.routes.logs import router as logs_router
    from adminbot.app.web.routes.soul_library import router as soul_library_router

    app.include_router(auth_router)
    app.include_router(dashboard_router)
    app.include_router(bots_router)
    app.include_router(logs_router)
    app.include_router(soul_library_router)
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    return app
