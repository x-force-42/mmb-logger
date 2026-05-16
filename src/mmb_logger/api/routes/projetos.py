"""Rotas /api/projetos."""

from __future__ import annotations

from fastapi import APIRouter, Request

from mmb_logger.db import get_conn, list_projetos
from mmb_logger.models import ProjetosListResponse

router = APIRouter(prefix="/api/projetos", tags=["projetos"])


@router.get("", response_model=ProjetosListResponse)
def list_projetos_route(request: Request) -> ProjetosListResponse:
    db_path = request.app.state.db_path
    with get_conn(db_path) as conn:
        items = list_projetos(conn)
    return ProjetosListResponse(items=items)
