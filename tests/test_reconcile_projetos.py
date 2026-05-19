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
