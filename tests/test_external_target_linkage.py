"""Testes do L7 — propagação correta de target externo.

Cobre os 3 sub-fixes da issue #40 (logger-external-target-linkage):

T8. `audit._find_ciclo_by_epic_project` resolve `project_short` → `repo`
    via `mmb_logger.targets.short_to_repo`, com fallback retrocompat pra
    ciclos antigos rotulados com `mmb-<short>`.
T9. `backfill.agent_sessions` resolve repo via mesmo helper (não mais
    hardcode `f"mmb-{short}"`).
T10. `derive.parse_closes` aceita o formato cross-repo
    `Closes owner/repo#N`, case-insensitive; `link_pr_to_issue` ignora
    cross-repo refs apontando para repo diferente, com warning.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mmb_logger.backfill.agent_sessions import SessionRecord, link_to_db_ciclo
from mmb_logger.db import get_conn
from mmb_logger.reconcile.audit import _find_ciclo_by_epic_project
from mmb_logger.reconcile.derive import CloseRef, parse_closes
from mmb_logger.reconcile.gh import GhPr
from mmb_logger.reconcile.reconcile import ReconcileResult, link_pr_to_issue
from mmb_logger.targets import short_to_repo

# ── Registry fixture ──────────────────────────────────────────────────


@pytest.fixture()
def mixed_registry(tmp_path: Path, monkeypatch) -> Path:
    """Registry com 1 target interno e 1 externo (campo-premiado).

    Limpa o cache module-level pra garantir reload deste registry no
    teste — outros testes podem ter carregado um registry diferente.
    """
    targets = [
        {
            "id": "cockpit",
            "dest": "cockpit",
            "repo": "mmb-cockpit",
            "local_path": "mmb-cockpit",
            "worker_profile": "project-orchestrator.md",
            "agent_layer": "project",
            "tracked_by_logger": True,
        },
        {
            "id": "campo-premiado",
            "dest": "campo-premiado",
            "repo": "campo-premiado",
            "local_path": "/abs/path/to/campo-premiado",
            "worker_profile": "project-orchestrator.md",
            "agent_layer": "project",
            "tracked_by_logger": True,
            "kind": "external",
            "owner": "x-force-42",
        },
    ]
    p = tmp_path / "targets.json"
    p.write_text(json.dumps({"schema_version": 1, "targets": targets}))
    monkeypatch.setenv("MMB_TARGETS_FILE", str(p))
    # Limpa cache module-level pra forçar reload.
    import mmb_logger.targets as mod
    mod._cache = None
    mod._cache_path = None
    yield p
    # Pós-teste: invalida cache pra não vazar pro próximo teste.
    mod._cache = None
    mod._cache_path = None


# ── T8: short_to_repo helper ──────────────────────────────────────────


def test_short_to_repo_internal_target(mixed_registry: Path):
    """Target interno: `cockpit` → `mmb-cockpit` (preserva semântica antiga)."""
    assert short_to_repo("cockpit") == "mmb-cockpit"


def test_short_to_repo_external_target(mixed_registry: Path):
    """Target externo: `campo-premiado` → `campo-premiado` (sem prefixo `mmb-`)."""
    assert short_to_repo("campo-premiado") == "campo-premiado"


def test_short_to_repo_unknown_fallback(mixed_registry: Path):
    """Shorts não-registrados caem no fallback retrocompat `mmb-<short>`."""
    assert short_to_repo("unknown-thing") == "mmb-unknown-thing"


def test_short_to_repo_registry_missing_fallback(tmp_path: Path, monkeypatch):
    """Sem registry resolvível, fallback retrocompat ainda funciona."""
    monkeypatch.setenv("MMB_TARGETS_FILE", str(tmp_path / "does-not-exist.json"))
    import mmb_logger.targets as mod
    mod._cache = None
    mod._cache_path = None
    assert short_to_repo("cockpit") == "mmb-cockpit"
    mod._cache = None
    mod._cache_path = None


# ── T8: audit lookup ──────────────────────────────────────────────────


def _seed_epico_ciclo(
    db_path: Path,
    *,
    epico_id: str,
    ciclo_id: str,
    project: str,
    planner_invoked_at: str = "2026-05-19T10:00:00Z",
) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO epicos (id, slug, started_at, intencao, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (epico_id, epico_id, planner_invoked_at, "test", "aberto"),
        )
        conn.execute(
            "INSERT INTO ciclos "
            "(id, epico_id, project, planner_invoked_at, status, instruction) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ciclo_id, epico_id, project, planner_invoked_at, "planejado", "x"),
        )


def test_audit_lookup_internal_target_unchanged(
    db_path: Path, mixed_registry: Path
):
    """Invariante interno: ciclos com `project='mmb-cockpit'` continuam casando."""
    _seed_epico_ciclo(
        db_path,
        epico_id="my-epic",
        ciclo_id="cyc-internal",
        project="mmb-cockpit",
    )
    with get_conn(db_path) as conn:
        assert (
            _find_ciclo_by_epic_project(conn, "my-epic", "cockpit") == "cyc-internal"
        )


def test_audit_lookup_external_target_resolves_via_registry(
    db_path: Path, mixed_registry: Path
):
    """T8 core: `project_short='campo-premiado'` casa ciclo com
    `project='campo-premiado'` (sem prefixo) via registry."""
    _seed_epico_ciclo(
        db_path,
        epico_id="my-epic",
        ciclo_id="cyc-external",
        project="campo-premiado",
    )
    with get_conn(db_path) as conn:
        got = _find_ciclo_by_epic_project(conn, "my-epic", "campo-premiado")
        assert got == "cyc-external"


def test_audit_lookup_external_fallback_retrocompat(
    db_path: Path, mixed_registry: Path
):
    """Fallback retrocompat: ciclo pré-PR#34 com `project='mmb-campo-premiado'`
    ainda casa (cobre rows não-normalizadas pelo backfill L10)."""
    _seed_epico_ciclo(
        db_path,
        epico_id="my-epic",
        ciclo_id="cyc-legacy",
        project="mmb-campo-premiado",
    )
    with get_conn(db_path) as conn:
        got = _find_ciclo_by_epic_project(conn, "my-epic", "campo-premiado")
        assert got == "cyc-legacy"


# ── T9: backfill link_to_db_ciclo ─────────────────────────────────────


def _make_session_record(
    *,
    project: str | None,
    candidate_pr_number: int | None,
    role: str = "atomic",
) -> SessionRecord:
    """Construtor mínimo de SessionRecord pra exercer link_to_db_ciclo."""
    return SessionRecord(
        session_id="sess",
        transcript_path="x",
        role=role,
        project=project,
        candidate_pr_number=candidate_pr_number,
    )


def test_backfill_link_external_target(db_path: Path, mixed_registry: Path):
    """Backfill linka sessão de target externo a ciclo via registry."""
    _seed_epico_ciclo(
        db_path,
        epico_id="my-epic",
        ciclo_id="cyc-ext",
        project="campo-premiado",
    )
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE ciclos SET pr_number = ? WHERE id = ?", (4, "cyc-ext")
        )
    rec = _make_session_record(project="campo-premiado", candidate_pr_number=4)
    with get_conn(db_path) as conn:
        link_to_db_ciclo(rec, conn)
    assert rec.ciclo_id == "cyc-ext"
    assert rec.epico_id == "my-epic"


def test_backfill_link_internal_unchanged(db_path: Path, mixed_registry: Path):
    """Targets internos continuam linkando como antes (mmb-cockpit)."""
    _seed_epico_ciclo(
        db_path,
        epico_id="my-epic",
        ciclo_id="cyc-int",
        project="mmb-cockpit",
    )
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE ciclos SET pr_number = ? WHERE id = ?", (10, "cyc-int")
        )
    rec = _make_session_record(project="cockpit", candidate_pr_number=10)
    with get_conn(db_path) as conn:
        link_to_db_ciclo(rec, conn)
    assert rec.ciclo_id == "cyc-int"
    assert rec.epico_id == "my-epic"


# ── T10: cross-repo Closes ────────────────────────────────────────────


def _pr(
    *,
    repo: str,
    number: int,
    body: str,
) -> GhPr:
    return GhPr(
        repo=repo,
        number=number,
        title="t",
        body=body,
        state="OPEN",
        url=f"https://github.com/x-force-42/{repo}/pull/{number}",
        created_at="2026-05-19T10:00:00Z",
        merged_at=None,
        head_ref_name=f"task/{number}",
        additions=0,
        deletions=0,
        changed_files=0,
    )


def test_link_pr_mono_repo_simple():
    """Caso 1: `Closes #N` mono-repo (sem owner/repo)."""
    pr = _pr(repo="mmb-logger", number=10, body="Closes #1")
    result = ReconcileResult()
    by_issue = link_pr_to_issue([pr], result)
    assert by_issue == {1: pr}
    assert not any("cross-repo" in w for w in result.warnings)


def test_link_pr_cross_repo_same_repo_links():
    """Caso 2: `Closes owner/repo#N` apontando para o mesmo repo do PR liga."""
    pr = _pr(
        repo="campo-premiado",
        number=4,
        body="Closes x-force-42/campo-premiado#3",
    )
    result = ReconcileResult()
    by_issue = link_pr_to_issue([pr], result)
    assert by_issue == {3: pr}


def test_link_pr_cross_repo_different_repo_skipped_with_warning():
    """Caso 3: `Closes owner/repo#N` apontando para repo diferente do PR
    não-liga e emite warning."""
    pr = _pr(
        repo="mmb-logger",
        number=4,
        body="Closes x-force-42/other-repo#7",
    )
    result = ReconcileResult()
    by_issue = link_pr_to_issue([pr], result)
    assert by_issue == {}
    assert any(
        "cross-repo-closes-skipped" in w
        and "other-repo#7" in w
        for w in result.warnings
    )


def test_link_pr_multiple_closes_filters_cross_repo():
    """Caso 4: PR com mistura — mono-repo + cross-repo mesmo + cross-repo
    diferente. Só os 2 primeiros viram linkagem."""
    pr = _pr(
        repo="mmb-logger",
        number=4,
        body=(
            "Closes #1\n"
            "Closes x-force-42/mmb-logger#2\n"
            "Closes x-force-42/other#3"
        ),
    )
    result = ReconcileResult()
    by_issue = link_pr_to_issue([pr], result)
    assert by_issue == {1: pr, 2: pr}
    assert any("other#3" in w for w in result.warnings)


def test_link_pr_case_insensitive():
    """Caso 5: case-insensitive — `closes`, `CLOSES`, `Fixes` todos casam."""
    pr1 = _pr(repo="mmb-logger", number=10, body="closes #1")
    pr2 = _pr(repo="mmb-logger", number=11, body="CLOSES #2")
    pr3 = _pr(repo="mmb-logger", number=12, body="Fixes #3")
    pr4 = _pr(
        repo="mmb-logger",
        number=13,
        body="FIXES X-Force-42/MMB-Logger#4",
    )
    result = ReconcileResult()
    by_issue = link_pr_to_issue([pr1, pr2, pr3, pr4], result)
    assert by_issue == {1: pr1, 2: pr2, 3: pr3}
    # PR 13 não casa: nome de repo "MMB-Logger" difere de "mmb-logger"
    # (comparação é case-sensitive em repo names, que é correto: GH trata
    # como diferentes para Closes cross-repo).
    assert any("MMB-Logger#4" in w for w in result.warnings)


def test_parse_closes_returns_close_refs():
    """parse_closes devolve CloseRef tuples; cross-repo populado quando presente."""
    refs = parse_closes("Closes #1\nfixes x-force-42/other#2")
    assert refs == [
        CloseRef(None, None, 1),
        CloseRef("x-force-42", "other", 2),
    ]
