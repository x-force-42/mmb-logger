"""Testes do reconcile GH-only (fase 1).

Fetchers de GitHub são injetados como funções; nenhum teste toca a rede.
"""

from __future__ import annotations

from pathlib import Path

from mmb_logger.db import get_conn, patch_ciclo
from mmb_logger.reconcile.derive import (
    epic_from_labels,
    parse_anchor,
    parse_closes,
)
from mmb_logger.reconcile.gh import GhIssue, GhPr
from mmb_logger.reconcile.reconcile import reconcile

# ── Helpers ────────────────────────────────────────────────────────


def _issue(
    repo: str = "mmb-core",
    number: int = 1,
    state: str = "OPEN",
    labels: tuple[str, ...] = ("task", "project:mmb-core", "epic:e1"),
    body: str = "",
    closed_at: str | None = None,
    title: str = "test issue",
    created_at: str = "2026-05-16T00:00:00Z",
) -> GhIssue:
    return GhIssue(
        repo=repo,
        number=number,
        title=title,
        body=body,
        state=state,
        labels=labels,
        created_at=created_at,
        closed_at=closed_at,
    )


def _pr(
    repo: str = "mmb-core",
    number: int = 10,
    state: str = "OPEN",
    merged_at: str | None = None,
    body: str = "Closes #1",
    url: str = "https://github.com/x-force-42/mmb-core/pull/10",
    head_ref_name: str = "task/1-foo",
    created_at: str = "2026-05-16T01:00:00Z",
    additions: int = 3,
    deletions: int = 1,
    changed_files: int = 2,
) -> GhPr:
    return GhPr(
        repo=repo,
        number=number,
        title="t",
        body=body,
        state=state,
        url=url,
        created_at=created_at,
        merged_at=merged_at,
        head_ref_name=head_ref_name,
        additions=additions,
        deletions=deletions,
        changed_files=changed_files,
    )


def _fetchers(
    issues_by_repo: dict[str, list[GhIssue]],
    prs_by_repo: dict[str, list[GhPr]],
):
    def fi(_owner: str, repo: str, **_kw) -> list[GhIssue]:
        return issues_by_repo.get(repo, [])

    def fp(_owner: str, repo: str, **_kw) -> list[GhPr]:
        return prs_by_repo.get(repo, [])

    return fi, fp


def _run(db_path: Path, issues, prs, *, reset: bool = False, repos=("mmb-core",)):
    """Helper de teste fase 1: injeta fase 2 vazia + tooling_root tmp pra isolamento.

    `tooling_root` aponta pra dir do db_path (sem inbox/logs/state dentro), então
    audit (fase 3) e intents leem dirs vazios — efeito no-op.
    """
    fi, fp = _fetchers(issues, prs)
    return reconcile(
        db_path=str(db_path),
        reset=reset,
        fetch_issues_fn=fi,
        fetch_prs_fn=fp,
        repos=repos,
        andaime_version_fn=lambda: None,
        # Isolamento do FS real:
        tooling_root=str(db_path.parent),  # tmp dir sem .tooling/
        claude_projects_root=str(db_path.parent),  # tmp dir sem transcripts
        briefings=[],
        briefings_malformed=[],
        journal_signals=[],
        agent_signals=[],
        now_epoch=0.0,
        targets_for_sync=[],
    )


# ── Unit: derive functions ─────────────────────────────────────────


def test_parse_anchor_present():
    body = (
        "<!-- mmb-cycle-key: e1/core/2026-05-16T01:53:10Z\n"
        "     mmb-briefing-file: 2026-05-16T01-53-10Z_master_briefing_x.md -->\n"
        "\n"
        "# real content"
    )
    a = parse_anchor(body)
    assert a is not None
    assert a.epic_slug == "e1"
    assert a.project_short == "core"
    assert a.briefing_ts == "2026-05-16T01:53:10Z"
    assert a.briefing_file == "2026-05-16T01-53-10Z_master_briefing_x.md"


def test_parse_anchor_absent():
    assert parse_anchor("") is None
    assert parse_anchor("# Issue body without anchor") is None


def test_parse_anchor_malformed_key():
    """Key sem 3 partes = inválida, devolve None."""
    body = "<!-- mmb-cycle-key: only-two/parts -->"
    assert parse_anchor(body) is None


def test_parse_closes_variations():
    assert parse_closes("Closes #42") == [42]
    assert parse_closes("fixes #1 and closes #2") == [1, 2]
    assert parse_closes("Resolves #99\nfix #100") == [99, 100]
    assert parse_closes("") == []
    assert parse_closes("issue 42 sem prefixo correto") == []


def test_epic_from_labels():
    assert epic_from_labels(("task", "epic:my-slug", "project:mmb-core")) == "my-slug"
    assert epic_from_labels(("task", "project:mmb-core")) is None
    assert epic_from_labels(()) is None


# ── Status derivation ──────────────────────────────────────────────


def test_status_planejado(db_path: Path):
    result = _run(db_path, {"mmb-core": [_issue()]}, {"mmb-core": []})
    assert result.ciclos_upserted == 1
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status, pr_url, pr_number, merged_to_main FROM ciclos"
        ).fetchone()
        assert row["status"] == "planejado"
        assert row["pr_url"] is None
        assert row["pr_number"] is None
        assert row["merged_to_main"] is None


def test_status_pr_aberto(db_path: Path):
    result = _run(
        db_path,
        {"mmb-core": [_issue()]},
        {"mmb-core": [_pr()]},
    )
    assert result.ciclos_upserted == 1
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status, pr_url, pr_number, merged_to_main, "
            "closed_partial_at, closed_complete_at FROM ciclos"
        ).fetchone()
        assert row["status"] == "pr_aberto"
        assert row["pr_number"] == 10
        assert row["pr_url"].endswith("/pull/10")
        assert row["merged_to_main"] == 0
        assert row["closed_partial_at"] == "2026-05-16T01:00:00Z"
        assert row["closed_complete_at"] is None


def test_status_completo(db_path: Path):
    _run(
        db_path,
        {"mmb-core": [_issue(state="CLOSED", closed_at="2026-05-16T02:00:00Z")]},
        {"mmb-core": [_pr(state="MERGED", merged_at="2026-05-16T01:30:00Z")]},
    )
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status, merged_to_main, closed_complete_at, "
            "diff_added, diff_deleted, diff_files FROM ciclos"
        ).fetchone()
        assert row["status"] == "completo"
        assert row["merged_to_main"] == 1
        assert row["closed_complete_at"] == "2026-05-16T01:30:00Z"
        assert row["diff_added"] == 3
        assert row["diff_deleted"] == 1
        assert row["diff_files"] == 2


def test_status_abortado(db_path: Path):
    _run(
        db_path,
        {"mmb-core": [_issue(state="CLOSED", closed_at="2026-05-16T02:00:00Z")]},
        {"mmb-core": []},
    )
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status, pr_url, merged_to_main FROM ciclos"
        ).fetchone()
        assert row["status"] == "abortado"
        assert row["pr_url"] is None
        assert row["merged_to_main"] is None


# ── Domain boundary: preservação humana ────────────────────────────


def test_preserves_assertiveness_score(db_path: Path):
    """Reconcile não pode sobrescrever assertiveness_score."""
    _run(db_path, {"mmb-core": [_issue()]}, {"mmb-core": []})
    with get_conn(db_path) as conn:
        cid = conn.execute("SELECT id FROM ciclos").fetchone()["id"]
        patch_ciclo(conn, cid, assertiveness_score=4)

    # Segundo run com mais informação (PR apareceu) — derivado muda, humano não
    _run(db_path, {"mmb-core": [_issue()]}, {"mmb-core": [_pr()]})
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status, assertiveness_score FROM ciclos"
        ).fetchone()
        assert row["status"] == "pr_aberto"  # derivado atualizou
        assert row["assertiveness_score"] == 4  # humano preservado


def test_preserves_review_note(db_path: Path):
    _run(db_path, {"mmb-core": [_issue()]}, {"mmb-core": []})
    with get_conn(db_path) as conn:
        cid = conn.execute("SELECT id FROM ciclos").fetchone()["id"]
        patch_ciclo(conn, cid, review_note="ótimo trabalho, mergeei rapidão")

    _run(db_path, {"mmb-core": [_issue()]}, {"mmb-core": []})
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT review_note FROM ciclos").fetchone()
        assert row["review_note"] == "ótimo trabalho, mergeei rapidão"


# ── Idempotência ───────────────────────────────────────────────────


def test_idempotent(db_path: Path):
    issues = {
        "mmb-core": [_issue(state="CLOSED", closed_at="2026-05-16T02:00:00Z")]
    }
    prs = {"mmb-core": [_pr(state="MERGED", merged_at="2026-05-16T01:30:00Z")]}

    _run(db_path, issues, prs)
    with get_conn(db_path) as conn:
        snap1_ciclos = [dict(r) for r in conn.execute("SELECT * FROM ciclos").fetchall()]
        snap1_epicos = [dict(r) for r in conn.execute("SELECT * FROM epicos").fetchall()]

    _run(db_path, issues, prs)
    with get_conn(db_path) as conn:
        snap2_ciclos = [dict(r) for r in conn.execute("SELECT * FROM ciclos").fetchall()]
        snap2_epicos = [dict(r) for r in conn.execute("SELECT * FROM epicos").fetchall()]

    assert snap1_ciclos == snap2_ciclos
    assert snap1_epicos == snap2_epicos


# ── Warnings ───────────────────────────────────────────────────────


def test_warning_pr_without_closes(db_path: Path):
    result = _run(
        db_path,
        {"mmb-core": [_issue()]},
        {"mmb-core": [_pr(body="só prosa, sem Closes")]},
    )
    assert any("pr-without-closes" in w for w in result.warnings)


def test_warning_missing_anchor(db_path: Path):
    result = _run(
        db_path,
        {"mmb-core": [_issue(body="# briefing sem âncora")]},
        {"mmb-core": []},
    )
    assert any("missing-anchor" in w for w in result.warnings)


def test_warning_issue_without_epic_label(db_path: Path):
    result = _run(
        db_path,
        {"mmb-core": [_issue(labels=("task", "project:mmb-core"))]},
        {"mmb-core": []},
    )
    assert any("issue-without-epic-label" in w for w in result.warnings)
    # E não cria ciclo
    with get_conn(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM ciclos").fetchone()["n"] == 0


def test_warning_multiple_prs_for_issue(db_path: Path):
    """Duas PRs apontando pra mesma issue → warning, escolhe o mergeado."""
    result = _run(
        db_path,
        {"mmb-core": [_issue()]},
        {
            "mmb-core": [
                _pr(number=10, body="Closes #1", created_at="2026-05-16T01:00:00Z"),
                _pr(
                    number=11,
                    body="Closes #1",
                    created_at="2026-05-16T02:00:00Z",
                    merged_at="2026-05-16T03:00:00Z",
                    state="MERGED",
                ),
            ]
        },
    )
    assert any("multiple-prs-for-issue" in w for w in result.warnings)
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT pr_number, status FROM ciclos").fetchone()
        assert row["pr_number"] == 11  # mergeado venceu
        assert row["status"] == "completo"


# ── Anchor-driven natural key ──────────────────────────────────────


def test_anchor_mismatch_epic(db_path: Path):
    """Âncora com epic divergente do label gera warning anchor-mismatch.

    A linkagem ciclo ↔ issue depende da âncora; divergência silenciosa
    corrompe o casamento. Warning é a barreira contra "inferência 2.0".
    """
    body = (
        "<!-- mmb-cycle-key: e2/core/2026-05-16T01:00:00Z -->\n"
        "# corpo do briefing"
    )
    # Issue tem label epic:e1 mas âncora diz e2 → mismatch
    result = _run(
        db_path,
        {"mmb-core": [_issue(body=body, labels=("task", "project:mmb-core", "epic:e1"))]},
        {"mmb-core": []},
    )
    matches = [w for w in result.warnings if "anchor-mismatch" in w and "epic=e2" in w]
    assert matches, f"esperava warning anchor-mismatch com epic=e2, vi: {result.warnings}"


def test_anchor_mismatch_project(db_path: Path):
    """Âncora com project divergente do repo varrido gera warning."""
    body = (
        "<!-- mmb-cycle-key: e1/aquarium/2026-05-16T01:00:00Z -->\n"
        "# corpo"
    )
    # Issue está em mmb-core (varremos core), mas âncora diz aquarium
    result = _run(
        db_path,
        {"mmb-core": [_issue(body=body)]},
        {"mmb-core": []},
    )
    matches = [
        w for w in result.warnings if "anchor-mismatch" in w and "project=aquarium" in w
    ]
    assert matches, f"esperava warning anchor-mismatch com project=aquarium, vi: {result.warnings}"


def test_anchor_drives_natural_key(db_path: Path):
    """Issue com âncora usa briefing_ts no ciclo_id, não issue.createdAt."""
    body = "<!-- mmb-cycle-key: e1/core/2026-05-10T10:00:00Z -->"
    _run(
        db_path,
        {"mmb-core": [_issue(body=body, created_at="2026-05-16T00:00:00Z")]},
        {"mmb-core": []},
    )
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT id, planner_invoked_at FROM ciclos").fetchone()
        assert row["id"] == "e1__core__2026-05-10T10:00:00Z"
        assert row["planner_invoked_at"] == "2026-05-10T10:00:00Z"


# ── Serialização Pydantic com NULL ─────────────────────────────────


def test_null_serializes_with_pydantic(db_path: Path):
    """Ciclo derivado com pr_*=NULL, cost_*=NULL etc valida no Ciclo Pydantic."""
    from mmb_logger.models import Ciclo

    _run(db_path, {"mmb-core": [_issue()]}, {"mmb-core": []})
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM ciclos").fetchone()
    d = dict(row)
    obj = Ciclo(
        id=d["id"],
        epico_id=d["epico_id"],
        project=d["project"],
        planner_invoked_at=d["planner_invoked_at"],
        status=d["status"],
        instruction=d["instruction"],
        pr_url=d["pr_url"],
        pr_number=d["pr_number"],
        closed_partial_at=d["closed_partial_at"],
        closed_complete_at=d["closed_complete_at"],
        merged_to_main=d["merged_to_main"],
        assertiveness_score=d["assertiveness_score"],
        cost_usd=d["cost_usd"],
        abort_origin=d["abort_origin"],
        abort_reason=d["abort_reason"],
        andaime_version=d["andaime_version"],
    )
    assert obj.pr_url is None
    assert obj.cost_usd is None
    assert obj.status == "planejado"


# ── --reset ────────────────────────────────────────────────────────


def test_reset_clears_ciclos_and_epicos_and_cascades_eventos(db_path: Path):
    _run(db_path, {"mmb-core": [_issue()]}, {"mmb-core": []})
    with get_conn(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM ciclos").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM epicos").fetchone()["n"] == 1
        # Injeta evento ligado pro ciclo pra verificar CASCADE
        conn.execute(
            "INSERT INTO eventos (ciclo_id, ts, kind, payload_json) "
            "VALUES ((SELECT id FROM ciclos LIMIT 1), 't', 'state_change', '{}')"
        )
        assert conn.execute("SELECT COUNT(*) AS n FROM eventos").fetchone()["n"] == 1

    # --reset sem novas issues
    _run(db_path, {"mmb-core": []}, {"mmb-core": []}, reset=True)
    with get_conn(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM ciclos").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM epicos").fetchone()["n"] == 0
        # Evento foi via CASCADE
        assert conn.execute("SELECT COUNT(*) AS n FROM eventos").fetchone()["n"] == 0


def test_reset_preserves_projetos(db_path: Path):
    """--reset não toca em projetos, processed_files, jsonl_cursor."""
    from mmb_logger.db import upsert_projeto

    with get_conn(db_path) as conn:
        upsert_projeto(
            conn,
            id="mmb-core",
            slug="mmb-core",
            name="Core",
            path="/x",
            repo_url=None,
        )
        n_before = conn.execute("SELECT COUNT(*) AS n FROM projetos").fetchone()["n"]

    _run(db_path, {"mmb-core": []}, {"mmb-core": []}, reset=True)
    with get_conn(db_path) as conn:
        n_after = conn.execute("SELECT COUNT(*) AS n FROM projetos").fetchone()["n"]
        assert n_after == n_before


# ── Issue sem `task` label é ignorada (não é issue do método) ──────


def test_issue_without_task_label_ignored(db_path: Path):
    result = _run(
        db_path,
        {"mmb-core": [_issue(labels=("epic:e1", "project:mmb-core"))]},
        {"mmb-core": []},
    )
    assert result.ciclos_upserted == 0
    with get_conn(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM ciclos").fetchone()["n"] == 0
