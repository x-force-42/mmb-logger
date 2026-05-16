"""Rotas /api/andaime-versions — tag discovery dinâmica para o cockpit."""

from __future__ import annotations

from fastapi import APIRouter, Request

from mmb_logger.db import get_conn, list_andaime_versions
from mmb_logger.models import AndaimeVersionsResponse

router = APIRouter(prefix="/api/andaime-versions", tags=["andaime-versions"])


@router.get("", response_model=AndaimeVersionsResponse)
def list_andaime_versions_route(request: Request) -> AndaimeVersionsResponse:
    db_path = request.app.state.db_path
    with get_conn(db_path) as conn:
        items = list_andaime_versions(conn)
    return AndaimeVersionsResponse(items=items)
