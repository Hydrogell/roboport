"""Shared server responses and API-specific error mapping."""
from __future__ import annotations

import sqlite3

from pydantic import BaseModel, Field

from ...i18n import tr
from ...runtime.docker import DockerError
from ...server_data import find_server, spec_from_row
from .auth import ApiAuthError


def load_server(db: sqlite3.Connection, uuid: str) -> sqlite3.Row:
    row = find_server(db, uuid)
    if not row:
        raise ApiAuthError(404, "not_found", tr("api_v1.server_not_found", uuid=repr(uuid)))
    return row


class StateResponse(BaseModel):
    uuid: str
    name: str
    state: str = Field(..., description="running | stopped | exited | created | missing | unknown")
    container_id: str | None = None
    started_at: str | None = None


def state_response(uuid: str, row, rt) -> dict:
    """Уплощённый снимок состояния после lifecycle-операции."""
    spec = spec_from_row(row)
    try:
        st = rt.status(spec)
    except DockerError:
        return {"uuid": uuid, "name": row["name"], "state": "unknown",
                "container_id": None, "started_at": None}
    return {
        "uuid": uuid, "name": row["name"],
        "state": st.state.value,
        "container_id": st.container_id,
        "started_at": st.started_at,
    }
