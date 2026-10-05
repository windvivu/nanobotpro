"""FastAPI application factory for nanobot web dashboard."""

import json
import time
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware

_WEB_DIR = Path(__file__).parent
_TEMPLATES_DIR = _WEB_DIR / "templates"
_STATIC_DIR = _WEB_DIR / "static"
_THEMES_DIR = _TEMPLATES_DIR / "themes"
DEFAULT_THEME = "matrix"
# Browser-side theme choice from the dashboard switcher; no cookie = follow the server theme.
THEME_COOKIE = "nanobot_theme"
# Version of the dashboard (Nanobot OS) in its header and login page: "1." + the upstream release
# it follows (0.3.5 -> 1.3.5). The package version (`nanobot --version`) stays the upstream base.
DASHBOARD_VERSION = "1.3.5"


def available_themes() -> list[str]:
    """Dashboard themes shipped in templates/themes/<name>/ (a theme needs at least head.html)."""
    if not _THEMES_DIR.is_dir():
        return [DEFAULT_THEME]
    return sorted(p.name for p in _THEMES_DIR.iterdir() if (p / "head.html").is_file())


def resolve_theme(name: str | None) -> str:
    """Return the requested theme if it is installed, otherwise the default theme.

    Only names of existing theme directories are accepted, so the value can safely
    be used to build template include paths.
    """
    requested = (name or "").strip().lower()
    if not requested:
        return DEFAULT_THEME
    themes = available_themes()
    if requested in themes:
        return requested
    logger.warning(
        "[Web] Unknown dashboard theme '{}', using '{}' (available: {})",
        name, DEFAULT_THEME, ", ".join(themes),
    )
    return DEFAULT_THEME


@lru_cache(maxsize=32)
def _load_theme_manifest(path: str, mtime: float) -> dict[str, str]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("[Web] Ignoring invalid theme manifest {}: {}", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, str)}


def theme_info(name: str) -> dict[str, str]:
    """Label and theme-specific copy/assets from templates/themes/<name>/theme.json.

    Every key is optional; templates pass a neutral default to theme_value().
    """
    info = {"label": name.replace("-", " ").title()}
    path = _THEMES_DIR / name / "theme.json"
    if path.is_file():
        info.update(_load_theme_manifest(str(path), path.stat().st_mtime))
    info["name"] = name
    return info


def install_theme_globals(env, server_theme: str = DEFAULT_THEME) -> None:
    """Register the theme helpers base.html relies on, on any Jinja environment.

    A page uses the browser's choice (THEME_COOKIE) when it names an installed theme,
    otherwise the server theme. The server theme lives in env.globals["ui_theme"], so
    changing it there (see /api/theme/default) takes effect on the next page view.
    """

    def _theme_for(request) -> str:
        cookies = getattr(request, "cookies", None) or {}
        chosen = cookies.get(THEME_COOKIE, "")
        return chosen if chosen in available_themes() else env.globals.get("ui_theme", server_theme)

    @pass_context
    def _current_theme(ctx) -> str:
        return _theme_for(ctx.get("request"))

    @pass_context
    def _theme_value(ctx, key: str, default: str = "") -> str:
        return theme_info(_theme_for(ctx.get("request"))).get(key, default)

    env.globals.update(
        ui_theme=server_theme,
        current_theme=_current_theme,
        theme_value=_theme_value,
        theme_info=theme_info,
        ui_themes=lambda: [theme_info(name) for name in available_themes()],
        theme_cookie=THEME_COOKIE,
    )


class AuthMiddleware(BaseHTTPMiddleware):
    """Redirect unauthenticated requests to /login when password is set."""

    async def _call_next_safely(self, request: Request, call_next):
        try:
            return await call_next(request)
        except RuntimeError as exc:
            # Starlette BaseHTTPMiddleware can raise this when the downstream
            # response stream closes before a response object is delivered.
            # Treat it as a client/request lifecycle abort, not a gateway crash.
            message = str(exc)
            if message in {
                "No response returned.",
                "ASGI callable returned without starting response.",
            }:
                try:
                    disconnected = await request.is_disconnected()
                except Exception:
                    disconnected = False
                logger.warning(
                    "[Web] Request ended without response for {} (client_disconnected={}, error='{}')",
                    request.url.path,
                    disconnected,
                    message,
                )
                return Response(status_code=499)
            raise

    async def dispatch(self, request: Request, call_next):
        from nanobot.web.auth import is_authenticated, is_public_path

        password = request.app.state.web_password
        path = request.url.path

        # No password = no auth required
        if not password:
            return await self._call_next_safely(request, call_next)

        # Allow public paths
        if is_public_path(path):
            return await self._call_next_safely(request, call_next)

        # Check auth
        if not is_authenticated(request):
            logger.debug(f"[AuthMiddleware] Redirecting {path} -> /login")
            return RedirectResponse(url="/login", status_code=302)

        return await self._call_next_safely(request, call_next)


def create_app(config, session_manager=None, agent=None, channel_manager=None, theme: str | None = None) -> FastAPI:
    """Create and configure the FastAPI web dashboard application.

    Args:
        config: Nanobot Config object.
        session_manager: SessionManager instance (optional).
        agent: AgentLoop instance (optional, for future use).
        channel_manager: ChannelManager instance (optional, for Zalo setup).
        theme: Dashboard theme name (e.g. from --theme). Falls back to
            config.gateway.web.theme, then to DEFAULT_THEME.

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
    app.state.templates.env.globals["nanobot_version"] = DASHBOARD_VERSION  # "Nanobot OS v..."

    # Dashboard theme (templates/themes/<name>/): --theme wins over config. A browser can
    # pick another installed theme with the header switcher (THEME_COOKIE).
    ui_theme = resolve_theme(theme or getattr(getattr(config.gateway, "web", None), "theme", None))
    app.state.ui_theme = ui_theme
    install_theme_globals(app.state.templates.env, ui_theme)
    # Dashboard UI language is a browser preference, independent from voice
    # recognition language in gateway.web.voice.
    from nanobot.web.i18n import install_i18n_globals
    install_i18n_globals(app.state.templates.env)
    logger.info("[Web] Dashboard theme: {}", ui_theme)

    @app.post("/api/theme/default")
    async def set_default_theme(request: Request):
        """Make an installed theme the server default: saved to gateway.web.theme, effective at once.

        Browsers that picked their own theme keep it. A later `--theme` still wins for that run.
        """
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        name = payload.get("theme") if isinstance(payload, dict) else None
        if not isinstance(name, str) or name not in available_themes():
            return JSONResponse({"success": False, "error": "unknown theme"}, status_code=400)

        from nanobot.config.loader import load_config, save_config

        saved = load_config()
        saved.gateway.web.theme = name
        save_config(saved)
        app.state.config.gateway.web.theme = name
        app.state.ui_theme = name
        app.state.templates.env.globals["ui_theme"] = name
        logger.info("[Web] Default dashboard theme set to '{}' from the dashboard", name)
        return JSONResponse({"success": True, "theme": name})

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
            create_csrf_token,
            create_session,
            get_cookie_name,
            get_csrf_cookie_name,
            is_rate_limited,
            record_auth_failure,
            record_auth_success,
            verify_password,
        )

        # A few wrong passwords lock the address out for a while, so the password cannot be found by
        # trying (custom, identity hardening). Counted apart from the fleet API's Basic auth: a mistyped
        # password does not block the fleet, and a fleet call does not clear the login failures.
        login_key = "login:" + (request.client.host if request.client else "unknown")
        if is_rate_limited(login_key):
            return app.state.templates.TemplateResponse(
                request, "login.html", {"error": "Too many attempts. Try again in a few minutes."}, status_code=429)
        if verify_password(password, web_password):
            record_auth_success(login_key)
            token = create_session()
            # Over HTTPS (directly or behind a proxy) the cookies are never sent in clear (custom)
            secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"
            response = RedirectResponse(url="/", status_code=302)
            response.set_cookie(
                key=get_cookie_name(),
                value=token,
                httponly=True,
                secure=secure,
                samesite="lax",
                max_age=24 * 60 * 60,
                path="/",
            )
            response.set_cookie(
                key=get_csrf_cookie_name(),
                value=create_csrf_token(),
                httponly=False,
                secure=secure,
                samesite="lax",
                max_age=24 * 60 * 60,
                path="/",
            )
            return response
        else:
            record_auth_failure(login_key)
            return app.state.templates.TemplateResponse(request, "login.html", {"error": "Invalid password. Please try again."})

    @app.get("/logout")
    async def logout(request: Request):
        """Clear session and redirect to login."""
        from nanobot.web.auth import clear_session, get_cookie_name, get_csrf_cookie_name

        cookie_name = get_cookie_name()
        token = request.cookies.get(cookie_name)
        if token:
            clear_session(token)
        response = RedirectResponse(url="/login", status_code=302)
        response.delete_cookie(key=cookie_name, path="/")
        response.delete_cookie(key=get_csrf_cookie_name(), path="/")
        return response

    # Register routes
    from nanobot.web.routes.agentic import router as agentic_router
    from nanobot.web.routes.api import router as api_router
    from nanobot.web.routes.chat import router as chat_router
    from nanobot.web.routes.config import router as config_router
    from nanobot.web.routes.cron import router as cron_router
    from nanobot.web.routes.dashboard import router as dashboard_router
    from nanobot.web.routes.memory import router as memory_router
    from nanobot.web.routes.memory_graph import router as memory_graph_router
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
    app.include_router(memory_graph_router)
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

