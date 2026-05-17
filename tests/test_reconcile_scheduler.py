"""Testes do cron interno do reconcile + endpoints /api/reconcile-status e /api/reconcile."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from mmb_logger.api import reconcile_scheduler
from mmb_logger.reconcile.reconcile import ReconcileResult


@pytest.fixture(autouse=True)
def _reset_scheduler() -> Iterator[None]:
    """Resetar estado module-level entre testes — scheduler é singleton."""
    reconcile_scheduler._reset_for_test()
    # Garante env limpo
    for k in ("MMB_LOGGER_RECONCILE_AUTO", "MMB_LOGGER_RECONCILE_INTERVAL"):
        os.environ.pop(k, None)
    yield
    reconcile_scheduler._reset_for_test()


def _fake_result(epicos: int = 1, ciclos: int = 2) -> ReconcileResult:
    r = ReconcileResult()
    r.epicos_upserted = epicos
    r.ciclos_upserted = ciclos
    return r


# ── start_scheduler() ────────────────────────────────────────────


def test_auto_disabled_does_not_start(db_path: Path) -> None:
    """MMB_LOGGER_RECONCILE_AUTO=0 → scheduler não inicia."""
    os.environ["MMB_LOGGER_RECONCILE_AUTO"] = "0"

    async def run() -> dict[str, Any]:
        reconcile_scheduler.start_scheduler(db_path=db_path)
        return reconcile_scheduler.get_status()

    status = asyncio.run(run())
    assert status["auto_enabled"] is False
    assert reconcile_scheduler._task is None


def test_auto_enabled_starts_task(db_path: Path) -> None:
    os.environ["MMB_LOGGER_RECONCILE_AUTO"] = "1"
    os.environ["MMB_LOGGER_RECONCILE_INTERVAL"] = "60"

    async def run() -> None:
        reconcile_scheduler.start_scheduler(db_path=db_path)
        assert reconcile_scheduler._task is not None
        assert reconcile_scheduler._task.done() is False
        await reconcile_scheduler.stop_scheduler()

    asyncio.run(run())
    status = reconcile_scheduler.get_status()
    assert status["auto_enabled"] is True
    assert status["interval_seconds"] == 60


def test_invalid_interval_falls_back_to_default() -> None:
    os.environ["MMB_LOGGER_RECONCILE_INTERVAL"] = "not-a-number"
    assert reconcile_scheduler.get_interval() == 300

    os.environ["MMB_LOGGER_RECONCILE_INTERVAL"] = "-50"
    assert reconcile_scheduler.get_interval() == 300

    os.environ["MMB_LOGGER_RECONCILE_INTERVAL"] = "0"
    assert reconcile_scheduler.get_interval() == 300


# ── _run_once: invoca reconcile, atualiza status ─────────────────


async def test_run_once_success_updates_status() -> None:
    with patch(
        "mmb_logger.api.reconcile_scheduler.reconcile",
        return_value=_fake_result(epicos=3, ciclos=7),
    ) as mock:
        await reconcile_scheduler._run_once()
    assert mock.called
    status = reconcile_scheduler.get_status()
    assert status["last_status"] == "ok"
    assert status["last_error_msg"] is None
    assert status["last_result"] == {"epicos_upserted": 3, "ciclos_upserted": 7, "warnings": 0}
    assert status["last_run_ts"] is not None
    assert status["running"] is False


async def test_run_once_exception_records_error_without_crashing() -> None:
    with patch(
        "mmb_logger.api.reconcile_scheduler.reconcile",
        side_effect=RuntimeError("simulated reconcile failure"),
    ):
        await reconcile_scheduler._run_once()
    status = reconcile_scheduler.get_status()
    assert status["last_status"] == "error"
    assert "simulated reconcile failure" in status["last_error_msg"]
    assert status["last_result"] is None
    assert status["running"] is False  # flag liberado mesmo em erro


# ── Concurrência: _running guard ─────────────────────────────────


async def test_concurrent_run_is_skipped() -> None:
    """Se _run_once é chamado enquanto outro está rodando, segundo é no-op."""
    call_count = {"n": 0}

    def slow_reconcile(*args: Any, **kwargs: Any) -> ReconcileResult:
        call_count["n"] += 1
        time.sleep(0.1)
        return _fake_result()

    with patch("mmb_logger.api.reconcile_scheduler.reconcile", side_effect=slow_reconcile):
        # Dispara duas chamadas concorrentes
        await asyncio.gather(
            reconcile_scheduler._run_once(),
            reconcile_scheduler._run_once(),
        )

    # A segunda deve ter sido skipada
    assert call_count["n"] == 1


async def test_trigger_reconcile_while_running_returns_409_payload() -> None:
    """trigger_reconcile() deve recusar duplicata e retornar accepted=False."""
    reconcile_scheduler._running = True
    try:
        result = await reconcile_scheduler.trigger_reconcile()
    finally:
        reconcile_scheduler._running = False
    assert result["accepted"] is False
    assert "em execução" in result["reason"]


# ── Loop reage a cancellation ────────────────────────────────────


async def test_scheduler_loop_cancellable() -> None:
    """stop_scheduler cancela a task limpamente."""
    os.environ["MMB_LOGGER_RECONCILE_AUTO"] = "1"
    os.environ["MMB_LOGGER_RECONCILE_INTERVAL"] = "60"
    reconcile_scheduler.start_scheduler()
    assert reconcile_scheduler._task is not None
    await reconcile_scheduler.stop_scheduler()
    assert reconcile_scheduler._task is None


# ── Endpoints HTTP ────────────────────────────────────────────────


def test_get_reconcile_status_returns_schema(client: TestClient) -> None:
    resp = client.get("/api/reconcile-status")
    assert resp.status_code == 200
    body = resp.json()
    for key in (
        "auto_enabled",
        "interval_seconds",
        "running",
        "last_run_ts",
        "last_status",
        "last_error_msg",
        "last_result",
    ):
        assert key in body


def test_post_reconcile_triggers_and_returns_202(client: TestClient) -> None:
    with patch(
        "mmb_logger.api.reconcile_scheduler.reconcile",
        return_value=_fake_result(epicos=1, ciclos=4),
    ):
        resp = client.post("/api/reconcile")
    assert resp.status_code == 202
    body = resp.json()
    assert body["accepted"] is True
    assert body["status"]["last_status"] == "ok"
    assert body["status"]["last_result"] == {
        "epicos_upserted": 1,
        "ciclos_upserted": 4,
        "warnings": 0,
    }


def test_post_reconcile_when_running_returns_409(client: TestClient) -> None:
    reconcile_scheduler._running = True
    try:
        resp = client.post("/api/reconcile")
    finally:
        reconcile_scheduler._running = False
    assert resp.status_code == 409
    body = resp.json()
    assert body["accepted"] is False
    assert "em execução" in body["reason"]


def test_get_health_still_works(client: TestClient) -> None:
    """Confirma que /api/health não foi afetado pelo registro do scheduler."""
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
