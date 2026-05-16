"""Testes da camada db.py."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from mmb_logger.db import (
    get_ciclo,
    get_epico,
    init_db,
    insert_evento,
    list_eventos_by_ciclo,
    list_projetos,
    metrics_overview,
    patch_ciclo,
    update_ciclo_status,
    upsert_ciclo,
    upsert_epico,
    upsert_projeto,
)


def test_init_db_idempotente(tmp_path: Path):
    p = tmp_path / "x.db"
    init_db(p)
    init_db(p)  # segunda chamada não deve quebrar
    conn = sqlite3.connect(p)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"epicos", "ciclos", "eventos", "projetos", "processed_files", "jsonl_cursor"} <= tables


def test_upsert_epico_e_ciclo(conn: sqlite3.Connection):
    upsert_epico(conn, id="ep1", slug="ep1", started_at="2026-01-01T00:00:00Z", intencao="x")
    assert upsert_ciclo(
        conn,
        id="c1",
        epico_id="ep1",
        project="mmb-cockpit",
        planner_invoked_at="2026-01-01T00:00:00Z",
        status="iniciado",
        instruction="...",
    )
    # Second insert with same id should be skipped.
    assert not upsert_ciclo(
        conn,
        id="c1",
        epico_id="ep1",
        project="mmb-cockpit",
        planner_invoked_at="2026-01-01T00:00:00Z",
        status="iniciado",
        instruction="...",
    )

    ep = get_epico(conn, "ep1")
    assert ep is not None
    assert ep["ciclos_total"] == 1


def test_update_ciclo_status_e_eventos(conn: sqlite3.Connection):
    upsert_epico(conn, id="ep1", slug="ep1", started_at="t", intencao="x")
    upsert_ciclo(
        conn,
        id="c1",
        epico_id="ep1",
        project="mmb-cockpit",
        planner_invoked_at="t",
        status="iniciado",
        instruction="i",
    )
    update_ciclo_status(conn, "c1", status="completo", closed_complete_at="t2")
    c = get_ciclo(conn, "c1")
    assert c is not None
    assert c["status"] == "completo"
    insert_evento(conn, ciclo_id="c1", ts="t", kind="state_change", payload={"k": "v"})
    eventos = list_eventos_by_ciclo(conn, "c1")
    assert len(eventos) == 1
    assert eventos[0]["payload"] == {"k": "v"}


def test_patch_ciclo(conn: sqlite3.Connection):
    upsert_epico(conn, id="ep1", slug="ep1", started_at="t", intencao="x")
    upsert_ciclo(
        conn,
        id="c1",
        epico_id="ep1",
        project="mmb-cockpit",
        planner_invoked_at="t",
        status="completo",
        instruction="i",
    )
    ok = patch_ciclo(conn, "c1", merged_to_main=1, assertiveness_score=4, review_note="bom")
    assert ok
    c = get_ciclo(conn, "c1")
    assert c["merged_to_main"] == 1
    assert c["assertiveness_score"] == 4
    assert c["review_note"] == "bom"


def test_projetos(conn: sqlite3.Connection):
    upsert_projeto(
        conn,
        id="mmb-core",
        slug="mmb-core",
        name="MMB Core",
        path="/x",
        created_at="t",
    )
    pj = list_projetos(conn)
    assert len(pj) == 1
    assert pj[0]["slug"] == "mmb-core"


def test_metrics_overview_vazio(conn: sqlite3.Connection):
    m = metrics_overview(conn, days=30)
    assert m["ciclos_total"] == 0
    assert m["epicos_total"] == 0
    assert m["taxa_abort"] == 0.0
    assert m["status_breakdown"]["completo"] == 0
