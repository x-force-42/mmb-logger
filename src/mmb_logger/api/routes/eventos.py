"""Rotas /api/ciclos/{id}/eventos."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from mmb_logger.db import get_ciclo, get_conn, list_eventos_by_ciclo
from mmb_logger.models import EventosListResponse

router = APIRouter(prefix="/api/ciclos", tags=["eventos"])


@router.get("/{ciclo_id}/eventos", response_model=EventosListResponse)
def list_eventos_route(request: Request, ciclo_id: str) -> EventosListResponse:
    db_path = request.app.state.db_path
    with get_conn(db_path) as conn:
        if get_ciclo(conn, ciclo_id) is None:
            raise HTTPException(status_code=404, detail="Ciclo não encontrado")
        items = list_eventos_by_ciclo(conn, ciclo_id)
    return EventosListResponse(items=items)
