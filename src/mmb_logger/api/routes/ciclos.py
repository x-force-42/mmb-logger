"""Rotas /api/ciclos."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from mmb_logger.db import get_ciclo, get_conn, list_ciclos, patch_ciclo
from mmb_logger.models import CicloDetail, CicloPatch, CiclosListResponse

router = APIRouter(prefix="/api/ciclos", tags=["ciclos"])


@router.get("", response_model=CiclosListResponse)
def list_ciclos_route(
    request: Request,
    epico: str | None = None,
    project: str | None = None,
    status: str | None = Query(
        default=None, pattern="^(iniciado|planejado|pr_aberto|completo|abortado)$"
    ),
    abort_origin: str | None = Query(default=None, pattern="^(heartbeat|manual|self|master)$"),
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
    order_by: str = Query(default="planner_invoked_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> CiclosListResponse:
    db_path = request.app.state.db_path
    with get_conn(db_path) as conn:
        items, total = list_ciclos(
            conn,
            epico=epico,
            project=project,
            status=status,
            abort_origin=abort_origin,
            date_from=date_from,
            date_to=date_to,
            order_by=order_by,
            order_dir=order_dir,
            limit=limit,
            offset=offset,
        )
    return CiclosListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{ciclo_id}", response_model=CicloDetail)
def get_ciclo_route(request: Request, ciclo_id: str) -> CicloDetail:
    db_path = request.app.state.db_path
    with get_conn(db_path) as conn:
        c = get_ciclo(conn, ciclo_id)
        if not c:
            raise HTTPException(status_code=404, detail="Ciclo não encontrado")
    return CicloDetail(**c)


@router.patch("/{ciclo_id}", response_model=CicloDetail)
def patch_ciclo_route(request: Request, ciclo_id: str, payload: CicloPatch) -> CicloDetail:
    db_path = request.app.state.db_path
    data = payload.model_dump(exclude_unset=True)
    with get_conn(db_path) as conn:
        if get_ciclo(conn, ciclo_id) is None:
            raise HTTPException(status_code=404, detail="Ciclo não encontrado")
        patch_ciclo(
            conn,
            ciclo_id,
            merged_to_main=data.get("merged_to_main"),
            assertiveness_score=data.get("assertiveness_score"),
            review_note=data.get("review_note"),
        )
        c = get_ciclo(conn, ciclo_id)
    if not c:  # defesa contra TOCTOU teórico
        raise HTTPException(status_code=404, detail="Ciclo desapareceu durante o patch")
    return CicloDetail(**c)
