"""Testes do bucketing diário em BRT no metrics_overview.

Cobre o fix de fuso descoberto no épico tz-cockpit-dashboard (2026-05-17):
storage em UTC mas agregação diária precisa refletir dia operacional
local (BRT/UTC-3), senão eventos rodados entre 21:00-23:59 BRT caem
no dia UTC seguinte e o operador vê contagem errada.

Casos de borda: 2 timestamps separados por 1h em torno do limite UTC,
que pertencem a dias BRT diferentes.
"""

from __future__ import annotations

import sqlite3

from mmb_logger.db import metrics_overview, upsert_ciclo, upsert_epico


def _seed_two_boundary_ciclos(conn: sqlite3.Connection) -> None:
    """Cria 2 ciclos em torno da meia-noite BRT:

    - c1: 2026-05-16T02:30:00Z  → 2026-05-15T23:30:00 BRT → dia BRT 2026-05-15
    - c2: 2026-05-16T03:30:00Z  → 2026-05-16T00:30:00 BRT → dia BRT 2026-05-16

    Antes do fix (substr cru em UTC), AMBOS caíam no bucket "2026-05-16".
    Depois do fix, devem cair em dias separados.
    """
    upsert_epico(
        conn, id="ep", slug="ep", started_at="2026-05-16T00:00:00Z", intencao="boundary"
    )
    upsert_ciclo(
        conn,
        id="c1",
        epico_id="ep",
        project="mmb-cockpit",
        planner_invoked_at="2026-05-16T02:30:00Z",  # 23:30 BRT do dia 15
        status="completo",
        instruction="boundary-1",
    )
    upsert_ciclo(
        conn,
        id="c2",
        epico_id="ep",
        project="mmb-cockpit",
        planner_invoked_at="2026-05-16T03:30:00Z",  # 00:30 BRT do dia 16
        status="completo",
        instruction="boundary-2",
    )
    # cost_usd vem do reconciler (fase 4) — pra teste, set direto.
    conn.execute("UPDATE ciclos SET cost_usd=1.50 WHERE id='c1'")
    conn.execute("UPDATE ciclos SET cost_usd=2.75 WHERE id='c2'")


def test_ciclos_por_dia_bucket_brt_at_boundary(conn: sqlite3.Connection) -> None:
    """ciclos_por_dia agrupa pelo dia BRT, não UTC."""
    _seed_two_boundary_ciclos(conn)

    # days grande pra cobrir o histórico todo do fixture.
    res = metrics_overview(conn, days=10000)

    ciclos_por_dia = {row["dia"]: row["n"] for row in res["ciclos_por_dia"]}

    # Esperado pós-fix: 1 em cada dia BRT.
    assert ciclos_por_dia.get("2026-05-15") == 1, (
        f"c1 (02:30 UTC = 23:30 BRT do dia 15) deveria estar em 2026-05-15. "
        f"buckets: {ciclos_por_dia}"
    )
    assert ciclos_por_dia.get("2026-05-16") == 1, (
        f"c2 (03:30 UTC = 00:30 BRT do dia 16) deveria estar em 2026-05-16. "
        f"buckets: {ciclos_por_dia}"
    )
    # Anti-regressão UTC: se viesse "2026-05-16: 2", é bucketing UTC errado.
    assert ciclos_por_dia.get("2026-05-16") != 2, (
        "regressão pra bucketing UTC detectada — ambos ciclos caíram no "
        "mesmo dia UTC quando deveriam estar em dias BRT distintos."
    )


def test_custo_por_dia_bucket_brt_at_boundary(conn: sqlite3.Connection) -> None:
    """custo_por_dia segue mesma regra de bucketing BRT que ciclos_por_dia."""
    _seed_two_boundary_ciclos(conn)

    res = metrics_overview(conn, days=10000)

    custo_por_dia = {row["dia"]: row["usd"] for row in res["custo_por_dia"]}

    assert custo_por_dia.get("2026-05-15") == 1.50, (
        f"c1 cost (1.50) deveria estar em 2026-05-15 BRT. buckets: {custo_por_dia}"
    )
    assert custo_por_dia.get("2026-05-16") == 2.75, (
        f"c2 cost (2.75) deveria estar em 2026-05-16 BRT. buckets: {custo_por_dia}"
    )


def test_bucket_brt_normal_business_hours(conn: sqlite3.Connection) -> None:
    """Sanidade fora da borda: 15:00 UTC = 12:00 BRT do mesmo dia."""
    upsert_epico(
        conn, id="ep", slug="ep", started_at="2026-05-16T00:00:00Z", intencao="x"
    )
    upsert_ciclo(
        conn,
        id="c_noon",
        epico_id="ep",
        project="mmb-cockpit",
        planner_invoked_at="2026-05-16T15:00:00Z",  # 12:00 BRT
        status="completo",
        instruction="noon",
    )

    res = metrics_overview(conn, days=10000)
    ciclos_por_dia = {row["dia"]: row["n"] for row in res["ciclos_por_dia"]}

    assert ciclos_por_dia.get("2026-05-16") == 1, (
        f"timestamp UTC de meio-dia local deveria cair em 2026-05-16. "
        f"buckets: {ciclos_por_dia}"
    )
