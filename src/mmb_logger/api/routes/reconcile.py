"""Rotas /api/reconcile-status e /api/reconcile.

`GET  /api/reconcile-status` — snapshot do cron interno (auto on/off,
intervalo, última execução, último status, último erro, resumo do
último resultado).

`POST /api/reconcile` — dispara reconcile imediato. Se já estiver em
execução, retorna 409 sem iniciar duplicata; caso contrário, executa
síncrono e retorna 202 com o status atualizado.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status

from mmb_logger.api import reconcile_scheduler

router = APIRouter(prefix="/api", tags=["reconcile"])


@router.get("/reconcile-status")
def reconcile_status_route() -> dict[str, Any]:
    """Snapshot consumível por cockpit/curl pra avaliar freshness do DB."""
    return reconcile_scheduler.get_status()


@router.post("/reconcile")
async def trigger_reconcile_route(response: Response) -> dict[str, Any]:
    """Dispara reconcile imediato.

    Códigos:
      - 202 Accepted: reconcile foi executado (síncrono nesta versão).
      - 409 Conflict: já havia execução em curso — não duplicado.
    """
    result = await reconcile_scheduler.trigger_reconcile()
    if not result.get("accepted"):
        response.status_code = status.HTTP_409_CONFLICT
    else:
        response.status_code = status.HTTP_202_ACCEPTED
    return result
