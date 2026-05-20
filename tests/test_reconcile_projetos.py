"""Testes do sync de `projetos` a partir do registry de targets (T1).

Cobre a feature plugada no `reconcile()` que UPSERT-a uma row em `projetos`
para cada target tracked_by_logger declarado em `.tooling/targets.json`.

Idempotência, preservação de entries históricos (ex.: `mmb-core` removido
do registry mas com ciclos persistidos) e convenção de naming (internal
`mmb-<id>`, external `<id>` sem prefixo).
"""

from __future__ import annotations

from pathlib import Path

from mmb_logger.db import get_conn, list_projetos, upsert_projeto
from mmb_logger.reconcile.gh import GhIssue, GhPr
from mmb_logger.reconcile.inbox import Briefing
from mmb_logger.reconcile.reconcile import reconcile
from mmb_logger.targets import Target


def _target(
    id: str,
    *,
    repo: str | None = None,
    kind: str = "internal",
    local_path: str | None = None,
    owner: str = "x-force-42",
) -> Target:
    return Target(
        id=id,
        dest=id,
        repo=repo if repo is not None else f"mmb-{id}",
        local_path=local_path or (f"mmb-{id}" if kind == "internal" else f"/abs/{id}"),
        worker_profile="project-orchestrator.md",
        agent_layer="project",
        tracked_by_logger=True,
        owner=owner,
        requires_github=True,
        kind=kind,
        managed_by_reset=(kind == "internal"),
    )


def _run_with_targets(db_path: Path, targets: list[Target]) -> None:
    def fi(_owner: str, repo: str, **_kw) -> list[GhIssue]:
        return []

    def fp(_owner: str, repo: str, **_kw) -> list[GhPr]:
        return []

    reconcile(
        db_path=str(db_path),
        fetch_issues_fn=fi,
        fetch_prs_fn=fp,
        repos=(),
        andaime_version_fn=lambda: None,
        tooling_root=str(db_path.parent),
        claude_projects_root=str(db_path.parent),
        briefings=[],
        briefings_malformed=[],
        journal_signals=[],
        agent_signals=[],
        now_epoch=0.0,
        targets_for_sync=targets,
    )


def test_sync_internal_targets_use_mmb_prefix(db_path: Path) -> None:
    targets = [
        _target("cockpit"),
        _target("aquarium"),
        _target("logger"),
    ]
    _run_with_targets(db_path, targets)

    with get_conn(db_path) as conn:
        rows = list_projetos(conn)
    slugs = {r["slug"] for r in rows}
    assert slugs == {"mmb-cockpit", "mmb-aquarium", "mmb-logger"}

    by_slug = {r["slug"]: r for r in rows}
    assert by_slug["mmb-cockpit"]["id"] == "mmb-cockpit"
    assert by_slug["mmb-cockpit"]["name"] == "MMB Cockpit"
    assert by_slug["mmb-cockpit"]["path"] == "mmb-cockpit"
    assert by_slug["mmb-cockpit"]["repo_url"] == (
        "git@github.com:x-force-42/mmb-cockpit.git"
    )


def test_sync_external_target_no_prefix(db_path: Path) -> None:
    targets = [
        _target(
            "campo-premiado",
            repo="campo-premiado",
            kind="external",
            local_path="/home/eliezer/vnt/ASUS/campo-premiado",
        ),
    ]
    _run_with_targets(db_path, targets)

    with get_conn(db_path) as conn:
        rows = list_projetos(conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "campo-premiado"
    assert row["slug"] == "campo-premiado"
    assert row["name"] == "Campo Premiado"
    assert row["path"] == "/home/eliezer/vnt/ASUS/campo-premiado"
    assert row["repo_url"] == "git@github.com:x-force-42/campo-premiado.git"


def test_sync_mixed_internal_and_external(db_path: Path) -> None:
    """Caso real (espelha targets.json em produção): 3 internos + 1 externo."""
    targets = [
        _target("cockpit"),
        _target("aquarium"),
        _target("logger"),
        _target(
            "campo-premiado",
            repo="campo-premiado",
            kind="external",
            local_path="/home/eliezer/vnt/ASUS/campo-premiado",
        ),
    ]
    _run_with_targets(db_path, targets)

    with get_conn(db_path) as conn:
        slugs = [r["slug"] for r in list_projetos(conn)]
    assert slugs == sorted(
        ["mmb-cockpit", "mmb-aquarium", "mmb-logger", "campo-premiado"]
    )


def test_sync_is_idempotent(db_path: Path) -> None:
    targets = [_target("cockpit"), _target("aquarium")]
    _run_with_targets(db_path, targets)
    _run_with_targets(db_path, targets)

    with get_conn(db_path) as conn:
        rows = list_projetos(conn)
    assert len(rows) == 2


def test_sync_preserves_obsolete_entries(db_path: Path) -> None:
    """`mmb-core` saiu do registry mas tem ciclos persistidos — não some."""
    with get_conn(db_path) as conn:
        upsert_projeto(
            conn,
            id="mmb-core",
            slug="mmb-core",
            name="MMB Core",
            path="/home/eliezer/llab/MMB/mmb-core",
            repo_url="git@github.com:x-force-42/mmb-core.git",
        )

    _run_with_targets(db_path, [_target("cockpit"), _target("logger")])

    with get_conn(db_path) as conn:
        slugs = {r["slug"] for r in list_projetos(conn)}
    assert "mmb-core" in slugs
    assert {"mmb-cockpit", "mmb-logger"} <= slugs


def test_sync_updates_metadata_on_rerun(db_path: Path) -> None:
    """Re-run com `path` alterado deve atualizar o entry (ON CONFLICT UPDATE)."""
    t = _target("cockpit", local_path="old/path")
    _run_with_targets(db_path, [t])

    with get_conn(db_path) as conn:
        before = next(r for r in list_projetos(conn) if r["slug"] == "mmb-cockpit")
    assert before["path"] == "old/path"

    t2 = _target("cockpit", local_path="mmb-cockpit")
    _run_with_targets(db_path, [t2])

    with get_conn(db_path) as conn:
        after = next(r for r in list_projetos(conn) if r["slug"] == "mmb-cockpit")
    assert after["path"] == "mmb-cockpit"
    assert after["created_at"] == before["created_at"]


def test_sync_uses_target_owner_when_present(db_path: Path) -> None:
    t = _target("cockpit", owner="some-other-org")
    _run_with_targets(db_path, [t])
    with get_conn(db_path) as conn:
        row = next(r for r in list_projetos(conn) if r["slug"] == "mmb-cockpit")
    assert row["repo_url"] == "git@github.com:some-other-org/mmb-cockpit.git"


# ── L10: ciclos.project derivado de target.repo (sem prefixo `mmb-` indevido) ──


_CAMPO_PREMIADO = _target(
    "campo-premiado",
    repo="campo-premiado",
    kind="external",
    local_path="/abs/campo-premiado",
)


def _run_reconcile_with_briefing(
    db_path: Path,
    *,
    briefing: Briefing,
    repo: str,
    targets: list[Target],
) -> None:
    """Reconcile com 1 briefing órfão (sem issue) → ciclo `iniciado`."""

    def fi(_owner: str, _repo: str, **_kw) -> list[GhIssue]:
        return []

    def fp(_owner: str, _repo: str, **_kw) -> list[GhPr]:
        return []

    reconcile(
        db_path=str(db_path),
        fetch_issues_fn=fi,
        fetch_prs_fn=fp,
        repos=(repo,),
        andaime_version_fn=lambda: None,
        tooling_root=str(db_path.parent),
        claude_projects_root=str(db_path.parent),
        briefings=[briefing],
        briefings_malformed=[],
        journal_signals=[],
        agent_signals=[],
        now_epoch=0.0,
        stale_threshold_s=3600,
        targets_for_sync=targets,
    )


def test_external_target_ciclo_project_sem_prefixo(db_path: Path) -> None:
    """Reconcile pra target externo (`campo-premiado`) projeta ciclo com
    `project='campo-premiado'`, não `mmb-campo-premiado`.
    """
    briefing = Briefing(
        path="/tmp/inbox/campo-premiado/2026-05-19_master_briefing_x.md",
        epic_slug="e1",
        project_short="campo-premiado",
        created="2026-05-19T10:00:00Z",
        subject="task-x",
        body="# brief",
    )
    _run_reconcile_with_briefing(
        db_path,
        briefing=briefing,
        repo="campo-premiado",
        targets=[_CAMPO_PREMIADO],
    )

    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT project FROM ciclos WHERE id = ?", (briefing.cycle_id,)
        ).fetchone()
    assert row is not None
    assert row["project"] == "campo-premiado"


def test_internal_target_ciclo_project_mantem_prefixo(db_path: Path) -> None:
    """Sanity-check de não-regressão: targets internos seguem com `mmb-<id>`
    porque o `repo` deles JÁ tem o prefixo. T1 não mexe nesse caso.
    """
    briefing = Briefing(
        path="/tmp/inbox/logger/2026-05-19_master_briefing_y.md",
        epic_slug="e2",
        project_short="logger",
        created="2026-05-19T10:00:00Z",
        subject="task-y",
        body="# brief",
    )
    _run_reconcile_with_briefing(
        db_path,
        briefing=briefing,
        repo="mmb-logger",
        targets=[_target("logger")],
    )

    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT project FROM ciclos WHERE id = ?", (briefing.cycle_id,)
        ).fetchone()
    assert row is not None
    assert row["project"] == "mmb-logger"


def _seed_ciclo_with_project(
    conn, *, project: str, cycle_id: str = "e1__campo-premiado__t"
) -> None:
    """Insere epico+ciclo direto pra simular estado legado pré-fix."""
    conn.execute(
        "INSERT INTO epicos (id, slug, started_at, intencao, status) "
        "VALUES ('e1', 'e1', '2026-05-19T10:00:00Z', 'e1', 'aberto')"
    )
    conn.execute(
        "INSERT INTO ciclos (id, epico_id, project, planner_invoked_at, "
        "status, instruction) VALUES (?, 'e1', ?, '2026-05-19T10:00:00Z', "
        "'iniciado', 't')",
        (cycle_id, project),
    )


def test_backfill_corrige_prefix_legado_em_target_externo(db_path: Path) -> None:
    """Backfill idempotente: ciclo com `project='mmb-campo-premiado'` (estado
    pré-fix) vira `project='campo-premiado'` após reconcile.
    """
    with get_conn(db_path) as conn:
        _seed_ciclo_with_project(conn, project="mmb-campo-premiado")

    # Reconcile com targets — repos=() pra não tentar projetar nada novo,
    # só roda o backfill.
    def fi(_owner: str, _repo: str, **_kw) -> list[GhIssue]:
        return []

    def fp(_owner: str, _repo: str, **_kw) -> list[GhPr]:
        return []

    reconcile(
        db_path=str(db_path),
        fetch_issues_fn=fi,
        fetch_prs_fn=fp,
        repos=(),
        andaime_version_fn=lambda: None,
        tooling_root=str(db_path.parent),
        claude_projects_root=str(db_path.parent),
        briefings=[],
        briefings_malformed=[],
        journal_signals=[],
        agent_signals=[],
        now_epoch=0.0,
        targets_for_sync=[_CAMPO_PREMIADO],
    )

    with get_conn(db_path) as conn:
        row = conn.execute("SELECT project FROM ciclos").fetchone()
    assert row["project"] == "campo-premiado"


def test_backfill_e_idempotente(db_path: Path) -> None:
    """Rodar o reconcile 2x não muda nada quando ciclos já estão corretos."""
    with get_conn(db_path) as conn:
        _seed_ciclo_with_project(conn, project="campo-premiado")

    def fi(_owner: str, _repo: str, **_kw) -> list[GhIssue]:
        return []

    def fp(_owner: str, _repo: str, **_kw) -> list[GhPr]:
        return []

    for _ in range(2):
        reconcile(
            db_path=str(db_path),
            fetch_issues_fn=fi,
            fetch_prs_fn=fp,
            repos=(),
            andaime_version_fn=lambda: None,
            tooling_root=str(db_path.parent),
            claude_projects_root=str(db_path.parent),
            briefings=[],
            briefings_malformed=[],
            journal_signals=[],
            agent_signals=[],
            now_epoch=0.0,
            targets_for_sync=[_CAMPO_PREMIADO],
        )

    with get_conn(db_path) as conn:
        rows = list(conn.execute("SELECT id, project FROM ciclos"))
    assert len(rows) == 1
    assert rows[0]["project"] == "campo-premiado"


def test_backfill_nao_toca_targets_internos(db_path: Path) -> None:
    """Backfill só age em targets cujo repo não começa com `mmb-`. Ciclo
    legítimo de target interno (project=`mmb-cockpit`) não é tocado.
    """
    with get_conn(db_path) as conn:
        _seed_ciclo_with_project(
            conn, project="mmb-cockpit", cycle_id="e1__cockpit__t"
        )

    def fi(_owner: str, _repo: str, **_kw) -> list[GhIssue]:
        return []

    def fp(_owner: str, _repo: str, **_kw) -> list[GhPr]:
        return []

    reconcile(
        db_path=str(db_path),
        fetch_issues_fn=fi,
        fetch_prs_fn=fp,
        repos=(),
        andaime_version_fn=lambda: None,
        tooling_root=str(db_path.parent),
        claude_projects_root=str(db_path.parent),
        briefings=[],
        briefings_malformed=[],
        journal_signals=[],
        agent_signals=[],
        now_epoch=0.0,
        targets_for_sync=[_target("cockpit"), _CAMPO_PREMIADO],
    )

    with get_conn(db_path) as conn:
        row = conn.execute("SELECT project FROM ciclos").fetchone()
    assert row["project"] == "mmb-cockpit"
