"""Scheduler interno do `serve` que dispara reconcile periodicamente.

Conveniência pra reduzir fricção do reconcile manual sem acoplar
andaime ↔ logger. A CLI `mmb-logger reconcile` continua sendo a
fonte de verdade operacional pra debug/emergência — este scheduler
só invoca a mesma função `reconcile()`.

Comportamento:
- Loop asyncio dispara `reconcile()` em executor (thread pool) a
  cada `MMB_LOGGER_RECONCILE_INTERVAL` segundos (default 300).
- Se a execução anterior ainda está rodando quando o tick chega,
  o tick é skipado (flag `_running`). Não acumula filas.
- Exceções no reconcile são capturadas, registradas em `_status`
  como `error` + mensagem, e o loop continua no próximo tick.
- Desligado via `MMB_LOGGER_RECONCILE_AUTO=0` (default ligado).
- Estado consultável via função `get_status()` — usado pela rota
  `GET /api/reconcile-status`.

Manual trigger: `trigger_reconcile()` é chamável de rota POST
`/api/reconcile`; respeita o mesmo flag `_running` (não duplica
execuções concorrentes).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mmb_logger.reconcile.reconcile import ReconcileResult, reconcile

logger = logging.getLogger(__name__)

# Estado module-level. Re-derivável; não precisa persistir.
_running: bool = False
_status: dict[str, Any] = {
    "auto_enabled": False,
    "interval_seconds": 0,
    "running": False,
    "last_run_ts": None,
    "last_status": None,
    "last_error_msg": None,
    "last_result": None,
}
_task: asyncio.Task[None] | None = None
_db_path: Path | None = None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _result_to_summary(result: ReconcileResult) -> dict[str, int]:
    return {
        "epicos_upserted": result.epicos_upserted,
        "ciclos_upserted": result.ciclos_upserted,
        "warnings": len(result.warnings),
    }


async def _run_once() -> None:
    """Executa uma rodada de reconcile em executor. Atualiza `_status`.

    Idempotente sob concorrência: se outra execução está em curso,
    apenas registra skip e retorna. Exceções são capturadas — loop
    não morre.
    """
    global _running
    if _running:
        logger.info("reconcile cron: tick skipado (execução anterior ainda em curso)")
        return
    _running = True
    _status["running"] = True
    try:
        result = await asyncio.to_thread(reconcile, db_path=str(_db_path) if _db_path else None)
        _status["last_run_ts"] = _now_iso()
        _status["last_status"] = "ok"
        _status["last_error_msg"] = None
        _status["last_result"] = _result_to_summary(result)
    except Exception as e:  # captura defensiva — loop não pode morrer
        _status["last_run_ts"] = _now_iso()
        _status["last_status"] = "error"
        _status["last_error_msg"] = f"{type(e).__name__}: {e}"
        _status["last_result"] = None
        logger.exception("reconcile cron: execução falhou")
    finally:
        _running = False
        _status["running"] = False


async def _scheduler_loop(interval: int) -> None:
    """Loop principal: aguarda interval segundos, dispara _run_once, repete."""
    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("reconcile cron: loop cancelado")
            raise
        await _run_once()


async def trigger_reconcile() -> dict[str, Any]:
    """Trigger manual via endpoint POST /api/reconcile.

    Se já estiver rodando, retorna sem disparar duplicata; o caller
    decide o HTTP code apropriado (409 vs 202).
    """
    if _running:
        return {"accepted": False, "reason": "reconcile já em execução", "status": _status.copy()}
    await _run_once()
    return {"accepted": True, "status": _status.copy()}


def get_status() -> dict[str, Any]:
    """Snapshot do estado atual do scheduler. Cópia rasa pra evitar
    mutação externa do dict module-level."""
    return _status.copy()


def is_auto_enabled() -> bool:
    """Lê env `MMB_LOGGER_RECONCILE_AUTO` (default ligado)."""
    val = os.environ.get("MMB_LOGGER_RECONCILE_AUTO", "1").strip().lower()
    return val not in ("0", "false", "no", "off", "")


def get_interval() -> int:
    """Lê env `MMB_LOGGER_RECONCILE_INTERVAL` (default 300s).

    Valores inválidos / não-positivos caem no default — defensivo
    contra config malformada que derrubaria o serve.
    """
    raw = os.environ.get("MMB_LOGGER_RECONCILE_INTERVAL", "300").strip()
    try:
        n = int(raw)
        return n if n > 0 else 300
    except ValueError:
        return 300


def start_scheduler(db_path: str | os.PathLike[str] | None = None) -> None:
    """Hook de startup do FastAPI: registra a task no event loop.

    No-op se `MMB_LOGGER_RECONCILE_AUTO=0`. Estado de `_status`
    sempre é preenchido (auto_enabled, interval_seconds) pra que
    `GET /api/reconcile-status` seja informativo mesmo com cron
    desligado.
    """
    global _task, _db_path
    _db_path = Path(db_path) if db_path else None
    auto = is_auto_enabled()
    interval = get_interval()
    _status["auto_enabled"] = auto
    _status["interval_seconds"] = interval
    if not auto:
        logger.info("reconcile cron: desligado por env MMB_LOGGER_RECONCILE_AUTO=0")
        return
    loop = asyncio.get_event_loop()
    _task = loop.create_task(_scheduler_loop(interval))
    logger.info("reconcile cron: ativado, intervalo=%ds", interval)


async def stop_scheduler() -> None:
    """Hook de shutdown do FastAPI: cancela a task. No-op se nunca iniciado."""
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None


# ── Hooks pra teste ────────────────────────────────────────────
#
# Testes precisam:
#   - resetar _status entre casos
#   - substituir a função reconcile por mock
#   - executar uma rodada controlada


def _reset_for_test() -> None:
    """Resetar estado module-level entre testes. NÃO usar em produção."""
    global _running, _task, _db_path
    _running = False
    _task = None
    _db_path = None
    _status.update(
        {
            "auto_enabled": False,
            "interval_seconds": 0,
            "running": False,
            "last_run_ts": None,
            "last_status": None,
            "last_error_msg": None,
            "last_result": None,
        }
    )
