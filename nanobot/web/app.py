"""FastAPI application factory for nanobot web dashboard."""

import time
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware

from nanobot import __version__

_WEB_DIR = Path(__file__).parent
_TEMPLATES_DIR = _WEB_DIR / "templates"
_STATIC_DIR = _WEB_DIR / "static"


class AuthMiddleware(BaseHTTPMiddleware):
    """Redirect unauthenticated requests to /login when password is set."""

    async def dispatch(self, request: Request, call_next):
        from nanobot.web.auth import is_authenticated, is_public_path

        password = request.app.state.web_password
        path = request.url.path

        logger.debug(f"[AuthMiddleware] path={path}, password_set={bool(password)}")

        # No password = no auth required
        if not password:
            return await call_next(request)

        # Allow public paths
        if is_public_path(path):
            return await call_next(request)

        # Check auth
        if not is_authenticated(request):
            logger.debug(f"[AuthMiddleware] Redirecting {path} -> /login")
            return RedirectResponse(url="/login", status_code=302)

        return await call_next(request)


def create_app(config, session_manager=None, agent=None, channel_manager=None) -> FastAPI:
    """Create and configure the FastAPI web dashboard application.

    Args:
        config: Nanobot Config object.
        session_manager: SessionManager instance (optional).
        agent: AgentLoop instance (optional, for future use).
        channel_manager: ChannelManager instance (optional, for Zalo setup).

    Returns:
        Configured FastAPI application.
    """
    app = FastAPI(
        title="Nanobot Dashboard",
        docs_url=None,      # Disable Swagger UI
        redoc_url=None,     # Disable ReDoc
        openapi_url=None,   # Disable OpenAPI schema
    )

    # Store references in app state for routes to access
    app.state.config = config
    app.state.session_manager = session_manager
    app.state.agent = agent
    app.state.channel_manager = channel_manager
    app.state.start_time = time.time()
    app.state.templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    app.state.templates.env.globals["nanobot_version"] = __version__

    # Register loguru sink for web console streaming
    from nanobot.web.log_sink import buffer_sink
    logger.add(buffer_sink, level="DEBUG", format="{message}")

    # Web password from config
    web_password = getattr(getattr(config.gateway, "web", None), "password", "")
    app.state.web_password = web_password
    logger.info(f"[Web] Auth {'enabled' if web_password else 'disabled'} (password={'***' if web_password else 'empty'})")

    # Unique cookie name per bot_id (prevents conflict when multiple bots on same host)
    from nanobot.web.auth import init_cookie_name
    bot_id = getattr(config.gateway, "bot_id", "") or ""
    cookie_name = init_cookie_name(bot_id)
    app.state.templates.env.globals["session_cookie_name"] = cookie_name
    logger.info(f"[Web] Cookie name: {cookie_name}")

    # Inject zalo_enabled as a callable so sidebar can read current state dynamically
    # (user may toggle zalo in /config without restarting — reads config fresh each call)
    def _zalo_enabled() -> bool:
        try:
            from nanobot.config.loader import load_config
            cfg = load_config()
            zalo_raw = getattr(cfg.channels, "zalo", None) or {}
            if isinstance(zalo_raw, dict):
                return bool(zalo_raw.get("enabled", False))
            return bool(getattr(zalo_raw, "enabled", False))
        except Exception:
            return False

    app.state.templates.env.globals["zalo_enabled"] = _zalo_enabled


    # --- Auth routes (register BEFORE middleware) ---
    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, error: str | None = None):
        """Show login page."""
        if not web_password:
            return RedirectResponse(url="/", status_code=302)
        return app.state.templates.TemplateResponse(request, "login.html", {"error": error})

    @app.post("/login")
    async def login_submit(request: Request, password: str = Form(...)):
        """Process login form."""
        from nanobot.web.auth import (
            create_session,
            get_cookie_name,
            verify_password,
        )

        if verify_password(password, web_password):
            token = create_session()
            response = RedirectResponse(url="/", status_code=302)
            response.set_cookie(
                key=get_cookie_name(),
                value=token,
                httponly=True,
                samesite="lax",
                max_age=24 * 60 * 60,
                path="/",
            )
            return response
        else:
            return app.state.templates.TemplateResponse(request, "login.html", {"error": "Invalid password. Please try again."})

    @app.get("/logout")
    async def logout(request: Request):
        """Clear session and redirect to login."""
        from nanobot.web.auth import clear_session, get_cookie_name

        cookie_name = get_cookie_name()
        token = request.cookies.get(cookie_name)
        if token:
            clear_session(token)
        response = RedirectResponse(url="/login", status_code=302)
        response.delete_cookie(key=cookie_name, path="/")
        return response

    # Register routes
    from nanobot.web.routes.agentic import router as agentic_router
    from nanobot.web.routes.api import router as api_router
    from nanobot.web.routes.chat import router as chat_router
    from nanobot.web.routes.config import router as config_router
    from nanobot.web.routes.cron import router as cron_router
    from nanobot.web.routes.dashboard import router as dashboard_router
    from nanobot.web.routes.memory import router as memory_router
    from nanobot.web.routes.oauth import router as oauth_router
    from nanobot.web.routes.profiles import router as profiles_router
    from nanobot.web.routes.sessions import router as sessions_router
    from nanobot.web.routes.skills import router as skills_router
    from nanobot.web.routes.workspace import router as workspace_router
    from nanobot.web.routes.zalo_setup import router as zalo_setup_router
    app.include_router(dashboard_router)
    app.include_router(config_router)
    app.include_router(sessions_router)
    app.include_router(workspace_router)
    app.include_router(memory_router)
    app.include_router(agentic_router)
    app.include_router(skills_router)
    app.include_router(cron_router)
    app.include_router(profiles_router)
    app.include_router(chat_router)
    app.include_router(zalo_setup_router)
    app.include_router(api_router)
    app.include_router(oauth_router)

    # Mount static files
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # Add auth middleware LAST (Starlette processes middlewares in LIFO order)
    app.add_middleware(AuthMiddleware)

    return app

