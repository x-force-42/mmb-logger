"""Rotas /api/metricas."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from mmb_logger.db import get_conn, metrics_overview
from mmb_logger.models import MetricasOverview

router = APIRouter(prefix="/api/metricas", tags=["metricas"])


@router.get("/overview", response_model=MetricasOverview)
def overview_route(
    request: Request, days: int = Query(default=30, ge=1, le=365)
) -> MetricasOverview:
    db_path = request.app.state.db_path
    with get_conn(db_path) as conn:
        data = metrics_overview(conn, days=days)
    return MetricasOverview(**data)
