"""Redirect browser forms back to their settings tab."""
from __future__ import annotations

import urllib.parse

from fastapi.responses import RedirectResponse


def settings_redirect(*, msg: str | None = None, err: str | None = None,
                      tab: str) -> RedirectResponse:
    """Return a 303 redirect preserving a settings-tab anchor."""
    query = ""
    if msg:
        query = "?msg=" + urllib.parse.quote(msg)
    elif err:
        query = "?err=" + urllib.parse.quote(err)
    fragment = f"#{tab}" if tab else ""
    return RedirectResponse(url=f"/settings{query}{fragment}", status_code=303)
