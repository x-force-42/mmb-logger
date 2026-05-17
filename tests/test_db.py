"""Testes da camada db.py."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from mmb_logger.db import (
    count_ciclos,
    count_eventos,
    count_projetos,
    get_ciclo,
    get_epico,
    init_db,
    insert_evento,
    list_andaime_versions,
    list_ciclos,
    list_epicos,
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


def _seed_epico_ciclo(
    conn: sqlite3.Connection,
    *,
    ep_id: str,
    ciclo_id: str,
    andaime_version: str | None,
) -> None:
    upsert_epico(
        conn,
        id=ep_id,
        slug=ep_id,
        started_at=f"2026-01-01T00:00:{int(ep_id[-1]):02d}Z",
        intencao="x",
        andaime_version=andaime_version,
    )
    upsert_ciclo(
        conn,
        id=ciclo_id,
        epico_id=ep_id,
        project="mmb-cockpit",
        planner_invoked_at=f"2026-01-01T00:00:{int(ciclo_id[-1]):02d}Z",
        status="iniciado",
        instruction="i",
        andaime_version=andaime_version,
    )


def test_list_epicos_filter_andaime_version_single(conn: sqlite3.Connection):
    _seed_epico_ciclo(conn, ep_id="ep1", ciclo_id="c1", andaime_version="v0.5.0")
    _seed_epico_ciclo(conn, ep_id="ep2", ciclo_id="c2", andaime_version="v0.6.0")
    _seed_epico_ciclo(conn, ep_id="ep3", ciclo_id="c3", andaime_version=None)

    items, total = list_epicos(conn, andaime_versions=["v0.6.0"])
    assert total == 1
    assert {ep["id"] for ep in items} == {"ep2"}


def test_list_epicos_filter_andaime_version_multi(conn: sqlite3.Connection):
    _seed_epico_ciclo(conn, ep_id="ep1", ciclo_id="c1", andaime_version="v0.5.0")
    _seed_epico_ciclo(conn, ep_id="ep2", ciclo_id="c2", andaime_version="v0.6.0")
    _seed_epico_ciclo(conn, ep_id="ep3", ciclo_id="c3", andaime_version="v0.7.0")

    items, total = list_epicos(conn, andaime_versions=["v0.5.0", "v0.6.0"])
    assert total == 2
    assert {ep["id"] for ep in items} == {"ep1", "ep2"}


def test_list_ciclos_filter_andaime_version_single(conn: sqlite3.Connection):
    _seed_epico_ciclo(conn, ep_id="ep1", ciclo_id="c1", andaime_version="v0.5.0")
    _seed_epico_ciclo(conn, ep_id="ep2", ciclo_id="c2", andaime_version="v0.6.0")

    items, total = list_ciclos(conn, andaime_versions=["v0.6.0"])
    assert total == 1
    assert {c["id"] for c in items} == {"c2"}


def test_list_ciclos_filter_andaime_version_multi(conn: sqlite3.Connection):
    _seed_epico_ciclo(conn, ep_id="ep1", ciclo_id="c1", andaime_version="v0.5.0")
    _seed_epico_ciclo(conn, ep_id="ep2", ciclo_id="c2", andaime_version="v0.6.0")
    _seed_epico_ciclo(conn, ep_id="ep3", ciclo_id="c3", andaime_version="v0.7.0")

    items, total = list_ciclos(conn, andaime_versions=["v0.5.0", "v0.7.0"])
    assert total == 2
    assert {c["id"] for c in items} == {"c1", "c3"}


def test_list_andaime_versions_distinct_and_sorted(conn: sqlite3.Connection):
    # Duplicatas e fora de ordem — esperamos distinct + semver desc.
    _seed_epico_ciclo(conn, ep_id="ep1", ciclo_id="c1", andaime_version="v0.5.0")
    _seed_epico_ciclo(conn, ep_id="ep2", ciclo_id="c2", andaime_version="v0.7.0")
    _seed_epico_ciclo(conn, ep_id="ep3", ciclo_id="c3", andaime_version="v0.6.0")
    _seed_epico_ciclo(conn, ep_id="ep4", ciclo_id="c4", andaime_version="v0.7.0")

    assert list_andaime_versions(conn) == ["v0.7.0", "v0.6.0", "v0.5.0"]


def test_list_andaime_versions_excludes_null(conn: sqlite3.Connection):
    _seed_epico_ciclo(conn, ep_id="ep1", ciclo_id="c1", andaime_version="v0.5.0")
    _seed_epico_ciclo(conn, ep_id="ep2", ciclo_id="c2", andaime_version=None)

    assert list_andaime_versions(conn) == ["v0.5.0"]


def test_list_andaime_versions_empty_db(conn: sqlite3.Connection):
    assert list_andaime_versions(conn) == []


def test_list_andaime_versions_handles_atypical_tags(conn: sqlite3.Connection):
    # v0 (sem .x.y), v0.1 (sem patch) e v1.0.0-rc1 (sufixo não numérico)
    # devem coexistir sem crash, ordenados em semver-desc por tuple
    # de inteiros (prefixo parseável).
    _seed_epico_ciclo(conn, ep_id="ep1", ciclo_id="c1", andaime_version="v0")
    _seed_epico_ciclo(conn, ep_id="ep2", ciclo_id="c2", andaime_version="v0.1")
    _seed_epico_ciclo(conn, ep_id="ep3", ciclo_id="c3", andaime_version="v1.0.0-rc1")
    _seed_epico_ciclo(conn, ep_id="ep4", ciclo_id="c4", andaime_version="v0.10.0")

    # Ordenação por tuple-de-int prefixo: (1,0,0) > (0,10,0) > (0,1) > (0,)
    assert list_andaime_versions(conn) == ["v1.0.0-rc1", "v0.10.0", "v0.1", "v0"]


def _ep(conn: sqlite3.Connection, ep_id: str, av: str | None) -> None:
    upsert_epico(
        conn,
        id=ep_id,
        slug=ep_id,
        started_at=f"2026-01-01T00:00:{int(ep_id[-1]):02d}Z",
        intencao="x",
        andaime_version=av,
    )


def _ci(conn: sqlite3.Connection, ciclo_id: str, ep_id: str, av: str | None) -> None:
    upsert_ciclo(
        conn,
        id=ciclo_id,
        epico_id=ep_id,
        project="p",
        planner_invoked_at=f"2026-01-01T00:00:{int(ciclo_id[-1]):02d}Z",
        status="iniciado",
        instruction="i",
        andaime_version=av,
    )


def test_list_andaime_versions_only_in_ciclos(conn: sqlite3.Connection):
    # Versão presente só em ciclos (epico vinculado tem version NULL) → aparece.
    _ep(conn, "ep1", None)
    _ci(conn, "c1", "ep1", "v1.0.0")

    assert list_andaime_versions(conn) == ["v1.0.0"]


def test_list_andaime_versions_only_in_epicos(conn: sqlite3.Connection):
    # Versão presente só em epicos (ciclo vinculado tem version NULL) → aparece.
    _ep(conn, "ep1", "v2.0.0")
    _ci(conn, "c1", "ep1", None)

    assert list_andaime_versions(conn) == ["v2.0.0"]


def test_list_andaime_versions_no_duplication_when_in_both(conn: sqlite3.Connection):
    # Versão presente em ciclos E epicos → aparece apenas 1 vez.
    _ep(conn, "ep1", "v3.0.0")
    _ci(conn, "c1", "ep1", "v3.0.0")

    assert list_andaime_versions(conn) == ["v3.0.0"]


def test_list_andaime_versions_realistic_mix(conn: sqlite3.Connection):
    # Replica estado do DB real: ciclos {v0.7.0, v0.9.0}, epicos {v0.5.0, v0.7.0, v0.8.0}.
    # União esperada: [v0.9.0, v0.8.0, v0.7.0, v0.5.0].
    _ep(conn, "ep1", "v0.5.0")
    _ci(conn, "c1", "ep1", None)

    _ep(conn, "ep2", "v0.7.0")
    _ci(conn, "c2", "ep2", "v0.7.0")

    _ep(conn, "ep3", "v0.8.0")
    _ci(conn, "c3", "ep3", None)

    _ep(conn, "ep4", None)
    _ci(conn, "c4", "ep4", "v0.9.0")

    assert list_andaime_versions(conn) == ["v0.9.0", "v0.8.0", "v0.7.0", "v0.5.0"]


def test_list_andaime_versions_null_in_both_excluded(conn: sqlite3.Connection):
    # NULL em ciclos E epicos → não vaza pro resultado.
    _ep(conn, "ep1", None)
    _ci(conn, "c1", "ep1", None)

    assert list_andaime_versions(conn) == []


def test_count_ciclos_empty(conn: sqlite3.Connection):
    assert count_ciclos(conn) == 0


def test_count_ciclos_populated(conn: sqlite3.Connection):
    upsert_epico(conn, id="ep1", slug="ep1", started_at="2026-05-10T10:00:00Z", intencao="x")
    upsert_ciclo(
        conn,
        id="c1",
        epico_id="ep1",
        project="mmb-cockpit",
        planner_invoked_at="2026-05-10T10:00:00Z",
        status="iniciado",
        instruction="i",
    )
    upsert_ciclo(
        conn,
        id="c2",
        epico_id="ep1",
        project="mmb-core",
        planner_invoked_at="2026-05-10T10:00:00Z",
        status="iniciado",
        instruction="i",
    )
    assert count_ciclos(conn) == 2


def test_count_projetos_empty(conn: sqlite3.Connection):
    assert count_projetos(conn) == 0


def test_count_projetos_populated(conn: sqlite3.Connection):
    upsert_projeto(
        conn,
        id="mmb-cockpit",
        slug="mmb-cockpit",
        name="MMB Cockpit",
        path="/x",
        created_at="2026-05-10T10:00:00Z",
    )
    upsert_projeto(
        conn,
        id="mmb-core",
        slug="mmb-core",
        name="MMB Core",
        path="/y",
        created_at="2026-05-10T10:00:00Z",
    )
    assert count_projetos(conn) == 2


def test_count_eventos_empty(conn: sqlite3.Connection):
    assert count_eventos(conn) == 0


def test_count_eventos_populated(conn: sqlite3.Connection):
    upsert_epico(conn, id="ep1", slug="ep1", started_at="2026-05-10T10:00:00Z", intencao="x")
    upsert_ciclo(
        conn,
        id="c1",
        epico_id="ep1",
        project="mmb-cockpit",
        planner_invoked_at="2026-05-10T10:00:00Z",
        status="iniciado",
        instruction="i",
    )
    insert_evento(conn, ciclo_id="c1", ts="2026-05-10T10:00:00Z", kind="msg_send")
    insert_evento(conn, ciclo_id="c1", ts="2026-05-10T10:00:01Z", kind="msg_send")
    insert_evento(conn, ciclo_id="c1", ts="2026-05-10T10:00:02Z", kind="msg_send")
    assert count_eventos(conn) == 3


def test_metrics_overview_vazio(conn: sqlite3.Connection):
    m = metrics_overview(conn, days=30)
    assert m["ciclos_total"] == 0
    assert m["epicos_total"] == 0
    assert m["taxa_abort"] == 0.0
    assert m["status_breakdown"]["completo"] == 0
