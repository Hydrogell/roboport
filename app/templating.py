"""Jinja configuration and the panel's localized date filters."""
from __future__ import annotations

import re
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from babel.dates import format_date, format_time, format_timedelta
from fastapi import Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from . import __version__, i18n, locale_web
from .db import connect
from .panel_config import panel_tz


def _format_instant(instant: datetime, with_rel: bool) -> str | Markup:
    local = instant.astimezone(panel_tz())
    locale = i18n.cldr_locale(i18n.current_language())
    date_str = format_date(local.date(), format="medium", locale=locale)
    time_str = format_time(local, format="medium", tzinfo=local.tzinfo, locale=locale)
    zone = local.tzname() or ""
    suffix = f" {zone}" if zone else ""
    if not with_rel:
        return f"{date_str} {time_str}{suffix}"

    delta = instant - datetime.now(timezone.utc)
    try:
        relative = format_timedelta(delta, threshold=1.0, add_direction=True, format="short", locale=locale)
    except KeyError:
        # Some CLDR locales omit the short future form.
        relative = format_timedelta(delta, threshold=1.0, add_direction=True, format="long", locale=locale)
    zone_html = (
        f'<span class="ht-tz muted" style="font-size:11px;margin-left:4px">{escape(zone)}</span>'
    ) if zone else ""
    title = local.strftime("%Y-%m-%d %H:%M:%S") + suffix
    return Markup(
        f'<span class="ht" title="{escape(title)}">'
        f'<span class="ht-date">{escape(date_str)}</span>'
        f'<span class="ht-time">{escape(time_str)}{zone_html}</span>'
        f'<span class="ht-rel muted small">{escape(relative)}</span></span>'
    )


def humantime(s: str | None, *, with_rel: bool = True) -> str | Markup:
    """Format SQLite or Docker ISO timestamps. A timestamp without a zone is UTC."""
    if not s:
        return "—"
    value = re.sub(r"(\.\d{6})\d+", r"\1", str(s).strip())
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return _format_instant(instant, with_rel)


def unixtime(ts, *, with_rel: bool = True) -> str | Markup:
    if ts is None or ts == 0 or ts == "":
        return "—"
    try:
        seconds = int(ts)
    except (ValueError, TypeError):
        return str(ts)
    try:
        instant = datetime.fromtimestamp(seconds, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return "—"
    return _format_instant(instant, with_rel)


def _request_context(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    servers = []
    if user is not None:
        with closing(connect()) as conn:
            servers = conn.execute("SELECT uuid, name FROM servers ORDER BY name").fetchall()
    return {
        **locale_web.template_context(request),
        "sidebar_servers": [dict(row) for row in servers],
        "current_user": user,
        "csrf_token": user.csrf_token if user else "",
        "csp_nonce": getattr(request.state, "csp_nonce", ""),
    }


def create_templates(root: Path) -> Jinja2Templates:
    templates = Jinja2Templates(directory=root / "templates", context_processors=[_request_context])
    static = root / "static"
    assets = [*(static / "css").glob("*.css"), *(static / "js").glob("*.js"), *(static / "icons").glob("*.svg")]
    version = max((asset.stat().st_mtime_ns for asset in assets), default=0)
    templates.env.globals.update(static_v=str(version), panel_version=__version__,
                                 t=i18n.tr, locale_code="ru", locale_dir="ltr")
    templates.env.filters.update(humantime=humantime, unixtime=unixtime)
    return templates
