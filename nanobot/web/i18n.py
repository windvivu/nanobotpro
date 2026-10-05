"""Small, dependency-free internationalisation layer for the web dashboard.

The dashboard is server-rendered, so the locale is kept in a browser cookie and
resolved for every request.  Catalogues are deliberately plain JSON files: this
keeps adding a language possible without introducing a translation framework or
making the bot's runtime configuration language dependent.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import pass_context

_I18N_DIR = Path(__file__).with_name("i18n")
DEFAULT_LOCALE = "en"
SUPPORTED_LOCALES = ("en", "vi")
LOCALE_COOKIE = "nanobot_ui_locale"


@lru_cache(maxsize=8)
def _load_catalog(locale: str) -> dict[str, str]:
    """Load one validated catalogue, returning an empty mapping on failure."""
    if locale not in SUPPORTED_LOCALES:
        return {}
    path = _I18N_DIR / f"{locale}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items() if isinstance(value, str)}


def resolve_locale(request: Any | None) -> str:
    """Return the validated browser locale, falling back to English."""
    cookies = getattr(request, "cookies", None) or {}
    requested = str(cookies.get(LOCALE_COOKIE, "")).strip().lower()
    return requested if requested in SUPPORTED_LOCALES else DEFAULT_LOCALE


def catalog(locale: str) -> dict[str, str]:
    """Return a complete catalogue with English fallback for missing keys."""
    english = _load_catalog(DEFAULT_LOCALE)
    if locale == DEFAULT_LOCALE:
        return dict(english)
    translated = dict(english)
    translated.update(_load_catalog(locale))
    return translated


def translate(locale: str, key: str, default: str | None = None, **values: object) -> str:
    """Translate *key*, falling back to English and then *default* or the key."""
    value = catalog(locale).get(key, default if default is not None else key)
    if values:
        try:
            value = value.format(**values)
        except (KeyError, IndexError, ValueError):
            # A malformed optional interpolation must never break page rendering.
            pass
    return value


def install_i18n_globals(env) -> None:
    """Install locale helpers used by every dashboard template."""

    @pass_context
    def _current_locale(ctx) -> str:
        return resolve_locale(ctx.get("request"))

    @pass_context
    def _translate(ctx, key: str, default: str | None = None, **values: object) -> str:
        return translate(_current_locale(ctx), key, default, **values)

    @pass_context
    def _translations(ctx) -> dict[str, str]:
        return catalog(_current_locale(ctx))

    env.globals.update(
        current_locale=_current_locale,
        tr=_translate,
        ui_translations=_translations,
        ui_locales=lambda: SUPPORTED_LOCALES,
        locale_cookie=LOCALE_COOKIE,
    )
