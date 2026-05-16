"""Rota /api/health/detailed — health check com contagens agregadas."""

from __future__ import annotations

from fastapi import APIRouter, Request

from mmb_logger.db import count_ciclos, count_eventos, count_projetos, get_conn
from mmb_logger.models import HealthDetailedResponse

router = APIRouter(prefix="/api/health/detailed", tags=["health"])


@router.get("", response_model=HealthDetailedResponse)
def health_detailed(request: Request) -> HealthDetailedResponse:
    db_path = request.app.state.db_path
    with get_conn(db_path) as conn:
        return HealthDetailedResponse(
            status="ok",
            db_path=str(db_path),
            ciclos_count=count_ciclos(conn),
            projetos_count=count_projetos(conn),
            eventos_count=count_eventos(conn),
        )
