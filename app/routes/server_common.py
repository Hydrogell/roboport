"""Shared conventions for server pages rendered as HTML."""
import sqlite3
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import RedirectResponse

from ..i18n import tr
from ..server_data import find_server


def require_server(db: sqlite3.Connection, uuid: str) -> sqlite3.Row:
    row = find_server(db, uuid)
    if row is None:
        raise HTTPException(404, tr("common.server_not_found"))
    return row


def server_tab_redirect(uuid: str, tab: str, *, msg: str | None = None,
                        err: str | None = None) -> RedirectResponse:
    location = f"/servers/{uuid}/{tab}"
    if msg:
        location += "?msg=" + quote(msg)
    elif err:
        location += "?err=" + quote(err)
    return RedirectResponse(location, status_code=303)
