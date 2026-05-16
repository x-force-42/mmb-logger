"""Rotas /api/epicos."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from mmb_logger.db import get_conn, get_epico, list_ciclos_by_epico, list_epicos
from mmb_logger.models import EpicoDetail, EpicosListResponse

router = APIRouter(prefix="/api/epicos", tags=["epicos"])


@router.get("", response_model=EpicosListResponse)
def list_epicos_route(
    request: Request,
    status: str | None = Query(default=None, pattern="^(aberto|fechado)$"),
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> EpicosListResponse:
    db_path = request.app.state.db_path
    with get_conn(db_path) as conn:
        items, total = list_epicos(
            conn,
            status=status,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
        )
    return EpicosListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{epico_id}", response_model=EpicoDetail)
def get_epico_route(request: Request, epico_id: str) -> EpicoDetail:
    db_path = request.app.state.db_path
    with get_conn(db_path) as conn:
        ep = get_epico(conn, epico_id)
        if not ep:
            raise HTTPException(status_code=404, detail="Épico não encontrado")
        ciclos = list_ciclos_by_epico(conn, epico_id)
    return EpicoDetail(**ep, ciclos=ciclos)
