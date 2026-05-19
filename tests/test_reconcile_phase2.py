"""Testes da fase 2 do reconcile: nascimento via inbox + aborto pré-GH.

Inputs (briefings, signals) são injetados via parâmetros do `reconcile()`;
nenhum teste lê o FS real.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from mmb_logger.db import get_conn, patch_ciclo
from mmb_logger.reconcile.gh import GhIssue, GhPr
from mmb_logger.reconcile.inbox import Briefing
from mmb_logger.reconcile.reconcile import reconcile

# Ts canônicos pros testes (ISO8601 UTC).
TS_BRIEFING = "2026-05-16T10:00:00Z"
TS_ISSUE = "2026-05-16T10:05:00Z"
TS_PR_OPEN = "2026-05-16T11:00:00Z"
TS_PR_MERGED = "2026-05-16T12:00:00Z"
TS_NOW = "2026-05-16T20:00:00Z"  # 10h depois de TS_BRIEFING

# Epoch computado dinamicamente (não hardcodear — confunde ano).
EPOCH_BRIEFING = datetime.fromisoformat("2026-05-16T10:00:00+00:00").timestamp()
NOW_EPOCH_10H_AFTER = EPOCH_BRIEFING + 36000  # 10h depois


# ── Builders ───────────────────────────────────────────────────────


def _briefing(
    epic: str = "e1",
    project: str = "core",
    created: str = TS_BRIEFING,
    subject: str = "task-x",
    body: str = "# Briefing body",
    path: str | None = None,
) -> Briefing:
    return Briefing(
        path=path or f"/tmp/inbox/{project}/{created}_master_briefing_{subject}.md",
        epic_slug=epic,
        project_short=project,
        created=created,
        subject=subject,
        body=body,
    )


def _issue_with_anchor(
    briefing: Briefing,
    *,
    repo: str | None = None,
    number: int = 1,
    state: str = "OPEN",
    closed_at: str | None = None,
    created_at: str = TS_ISSUE,
    extra_body: str = "# Issue body real content",
) -> GhIssue:
    repo = repo or f"mmb-{briefing.project_short}"
    body = (
        f"<!-- mmb-cycle-key: {briefing.cycle_key} -->\n"
        f"\n{extra_body}"
    )
    return GhIssue(
        repo=repo,
        number=number,
        title=briefing.subject,
        body=body,
        state=state,
        labels=("task", f"project:{repo}", f"epic:{briefing.epic_slug}"),
        created_at=created_at,
        closed_at=closed_at,
    )


def _pr(
    *,
    repo: str = "mmb-core",
    number: int = 10,
    state: str = "OPEN",
    merged_at: str | None = None,
    issue_num: int = 1,
    created_at: str = TS_PR_OPEN,
) -> GhPr:
    return GhPr(
        repo=repo,
        number=number,
        title="feat: x",
        body=f"Closes #{issue_num}",
        state=state,
        url=f"https://github.com/x-force-42/{repo}/pull/{number}",
        created_at=created_at,
        merged_at=merged_at,
        head_ref_name="task/1-x",
        additions=10,
        deletions=2,
        changed_files=3,
    )


def _fetchers(issues_by_repo, prs_by_repo):
    def fi(_owner, repo, **_kw):
        return issues_by_repo.get(repo, [])

    def fp(_owner, repo, **_kw):
        return prs_by_repo.get(repo, [])

    return fi, fp


def _run(
    db_path: Path,
    *,
    issues=None,
    prs=None,
    briefings=None,
    briefings_malformed=None,
    journal_signals=None,
    agent_signals=None,
    repos=("mmb-core",),
    now_epoch: float = NOW_EPOCH_10H_AFTER,
    stale_threshold_s: int = 3600,
):
    issues = issues or {}
    prs = prs or {}
    fi, fp = _fetchers(issues, prs)
    return reconcile(
        db_path=str(db_path),
        fetch_issues_fn=fi,
        fetch_prs_fn=fp,
        repos=repos,
        andaime_version_fn=lambda: None,
        tooling_root=str(db_path.parent),  # tmp dir, audit/intents no-op
        claude_projects_root=str(db_path.parent),  # tmp dir, transcripts no-op
        briefings=briefings or [],
        briefings_malformed=briefings_malformed or [],
        journal_signals=journal_signals or [],
        agent_signals=agent_signals or [],
        now_epoch=now_epoch,
        stale_threshold_s=stale_threshold_s,
        targets_for_sync=[],
    )


# ── Nascimento via briefing ────────────────────────────────────────


def test_briefing_sem_issue_cria_iniciado(db_path: Path):
    """Briefing master→planner sem issue casada → ciclo `iniciado`."""
    b = _briefing()
    # now_epoch < briefing + threshold → nenhum sinal, mantém iniciado
    _run(
        db_path,
        briefings=[b],
        now_epoch=EPOCH_BRIEFING + 60,  # 1 min depois — não stale
    )
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT id, status, planner_invoked_at, briefing_md, abort_origin FROM ciclos"
        ).fetchone()
        assert row is not None
        assert row["id"] == b.cycle_id
        assert row["status"] == "iniciado"
        assert row["planner_invoked_at"] == TS_BRIEFING
        assert row["briefing_md"] == b.body
        assert row["abort_origin"] is None


def test_briefing_e_issue_evolui_para_planejado(db_path: Path):
    """Briefing + issue OPEN (anchor casa) → ciclo evolui para `planejado`."""
    b = _briefing()
    issue = _issue_with_anchor(b)
    _run(
        db_path,
        briefings=[b],
        issues={"mmb-core": [issue]},
        now_epoch=EPOCH_BRIEFING + 600,
    )
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status, briefing_md, planner_invoked_at FROM ciclos"
        ).fetchone()
        assert row["status"] == "planejado"
        # briefing_md vem do briefing (não do body da issue)
        assert row["briefing_md"] == b.body
        # planner_invoked_at = briefing.created (não issue.created_at)
        assert row["planner_invoked_at"] == TS_BRIEFING


def test_briefing_e_pr_aberto(db_path: Path):
    """Briefing + issue + PR sem merge → `pr_aberto`."""
    b = _briefing()
    issue = _issue_with_anchor(b)
    pr = _pr(issue_num=issue.number)
    _run(
        db_path,
        briefings=[b],
        issues={"mmb-core": [issue]},
        prs={"mmb-core": [pr]},
        now_epoch=EPOCH_BRIEFING + 600,
    )
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status, pr_number, briefing_md FROM ciclos"
        ).fetchone()
        assert row["status"] == "pr_aberto"
        assert row["pr_number"] == 10
        assert row["briefing_md"] == b.body


def test_briefing_pr_mergeado_completo(db_path: Path):
    """Briefing + issue + PR merged → `completo`."""
    b = _briefing()
    issue = _issue_with_anchor(b, state="CLOSED", closed_at=TS_PR_MERGED)
    pr = _pr(issue_num=issue.number, state="MERGED", merged_at=TS_PR_MERGED)
    _run(
        db_path,
        briefings=[b],
        issues={"mmb-core": [issue]},
        prs={"mmb-core": [pr]},
        now_epoch=EPOCH_BRIEFING + 7200,
    )
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status, merged_to_main, closed_complete_at, briefing_md FROM ciclos"
        ).fetchone()
        assert row["status"] == "completo"
        assert row["merged_to_main"] == 1
        assert row["closed_complete_at"] == TS_PR_MERGED
        assert row["briefing_md"] == b.body


# ── Aborto pré-GH ──────────────────────────────────────────────────


def test_briefing_sem_issue_com_worker_timeout_vira_abortado(db_path: Path):
    """journal commd-worker-timeout dentro da janela → abortado/worker-timeout."""
    from mmb_logger.reconcile.abort import _JournalWorkerSignal

    b = _briefing()
    sig = _JournalWorkerSignal(
        ts="2026-05-16T10:30:00Z",  # 30 min depois
        ev="commd-worker-timeout",
        dest="core",
    )
    _run(
        db_path,
        briefings=[b],
        journal_signals=[sig],
        now_epoch=EPOCH_BRIEFING + 1900,  # 31 min depois, ainda dentro threshold
        stale_threshold_s=3600,
    )
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status, abort_origin, abort_reason, abort_at FROM ciclos"
        ).fetchone()
        assert row["status"] == "abortado"
        assert row["abort_origin"] == "worker-timeout"
        assert row["abort_at"] == "2026-05-16T10:30:00Z"


def test_briefing_sem_issue_com_worker_exit_vira_abortado(db_path: Path):
    from mmb_logger.reconcile.abort import _JournalWorkerSignal

    b = _briefing()
    sig = _JournalWorkerSignal(
        ts="2026-05-16T10:15:00Z",
        ev="commd-worker-exit",
        dest="core",
    )
    _run(
        db_path,
        briefings=[b],
        journal_signals=[sig],
        now_epoch=EPOCH_BRIEFING + 1000,
        stale_threshold_s=3600,
    )
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT status, abort_origin FROM ciclos").fetchone()
        assert row["status"] == "abortado"
        assert row["abort_origin"] == "worker-exit"


def test_briefing_sem_issue_com_deregister_heartbeat(db_path: Path):
    """agents deregister com reason heartbeat → abortado/heartbeat."""
    from mmb_logger.reconcile.abort import _AgentDeregisterSignal

    b = _briefing()
    sig = _AgentDeregisterSignal(
        ts="2026-05-16T10:20:00Z",
        id="core-X1",
        project_short="core",
        reason="heartbeat-timeout (no progress 600s)",
        abort_origin="heartbeat",
    )
    _run(
        db_path,
        briefings=[b],
        agent_signals=[sig],
        now_epoch=EPOCH_BRIEFING + 1500,
        stale_threshold_s=3600,
    )
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status, abort_origin, abort_reason FROM ciclos"
        ).fetchone()
        assert row["status"] == "abortado"
        assert row["abort_origin"] == "heartbeat"
        assert "heartbeat-timeout" in row["abort_reason"]


def test_briefing_sem_issue_e_stale_vira_abortado(db_path: Path):
    """Briefing órfão > threshold sem sinais → abortado/stale.

    Valida também que abort_reason é explícito o bastante para o cockpit/Rick
    entender: menciona threshold, idade, criação, e a natureza inferida.
    """
    b = _briefing()
    _run(
        db_path,
        briefings=[b],
        # now é 10h depois — bem além do threshold default
        now_epoch=NOW_EPOCH_10H_AFTER,
        stale_threshold_s=3600,
    )
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status, abort_origin, abort_reason FROM ciclos"
        ).fetchone()
        assert row["status"] == "abortado"
        assert row["abort_origin"] == "stale"
        reason = row["abort_reason"]
        # Mensagem precisa carregar contexto suficiente pra reviewer humano:
        assert "stale" in reason
        assert "sem issue casada" in reason
        assert "sem sinal colateral" in reason
        assert "3600s" in reason  # threshold concreto
        assert b.created in reason  # quando foi dispatched
        assert "ausência+tempo" in reason  # natureza inferida
        assert "reclassificar" in reason  # cabe a revisor


def test_briefing_sem_issue_dentro_do_threshold_fica_iniciado(db_path: Path):
    """Briefing recente sem sinal nem além do threshold → continua iniciado."""
    b = _briefing()
    _run(
        db_path,
        briefings=[b],
        now_epoch=EPOCH_BRIEFING + 100,  # 100s depois — bem dentro do threshold
        stale_threshold_s=3600,
    )
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT status, abort_origin FROM ciclos").fetchone()
        assert row["status"] == "iniciado"
        assert row["abort_origin"] is None


# ── Preservação humana + idempotência ──────────────────────────────


def test_preserves_human_columns_through_fase2(db_path: Path):
    """assertiveness_score + review_note sobrevivem ao reconcile fase 2."""
    b = _briefing()
    _run(db_path, briefings=[b], now_epoch=EPOCH_BRIEFING + 60)
    with get_conn(db_path) as conn:
        cid = conn.execute("SELECT id FROM ciclos").fetchone()["id"]
        patch_ciclo(conn, cid, assertiveness_score=5, review_note="excelente briefing")

    # Run de novo, agora com issue (ciclo evolui de iniciado→planejado)
    issue = _issue_with_anchor(b)
    _run(
        db_path,
        briefings=[b],
        issues={"mmb-core": [issue]},
        now_epoch=EPOCH_BRIEFING + 600,
    )
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status, assertiveness_score, review_note FROM ciclos"
        ).fetchone()
        assert row["status"] == "planejado"  # derivado mudou
        assert row["assertiveness_score"] == 5  # humano preservado
        assert row["review_note"] == "excelente briefing"  # humano preservado


def test_idempotent_fase2(db_path: Path):
    """Dois runs com mesmo input produzem estado idêntico."""
    b = _briefing()
    issue = _issue_with_anchor(b)

    _run(
        db_path,
        briefings=[b],
        issues={"mmb-core": [issue]},
        now_epoch=EPOCH_BRIEFING + 600,
    )
    with get_conn(db_path) as conn:
        snap1_c = [dict(r) for r in conn.execute("SELECT * FROM ciclos")]
        snap1_e = [dict(r) for r in conn.execute("SELECT * FROM epicos")]

    _run(
        db_path,
        briefings=[b],
        issues={"mmb-core": [issue]},
        now_epoch=EPOCH_BRIEFING + 600,
    )
    with get_conn(db_path) as conn:
        snap2_c = [dict(r) for r in conn.execute("SELECT * FROM ciclos")]
        snap2_e = [dict(r) for r in conn.execute("SELECT * FROM epicos")]

    assert snap1_c == snap2_c
    assert snap1_e == snap2_e


def test_idempotent_iniciado_para_planejado_e_volta(db_path: Path):
    """Issue some + briefing fica: ciclo volta pra iniciado limpo, sem lixo."""
    b = _briefing()
    issue = _issue_with_anchor(b)

    # 1º run: iniciado → planejado
    _run(
        db_path,
        briefings=[b],
        issues={"mmb-core": [issue]},
        now_epoch=EPOCH_BRIEFING + 600,
    )
    # 2º run: issue desapareceu (foi deletada). Briefing fica.
    _run(
        db_path,
        briefings=[b],
        issues={"mmb-core": []},
        now_epoch=EPOCH_BRIEFING + 700,  # ainda dentro threshold → iniciado
    )
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status, pr_url, pr_number, merged_to_main FROM ciclos"
        ).fetchone()
        assert row["status"] == "iniciado"
        # Campos derivados de PR voltam pra NULL
        assert row["pr_url"] is None
        assert row["pr_number"] is None
        assert row["merged_to_main"] is None


# ── Warnings ───────────────────────────────────────────────────────


def test_warning_briefing_malformed(db_path: Path):
    """Briefing malformado (passado em briefings_malformed) gera warning."""
    result = _run(
        db_path,
        briefings=[],
        briefings_malformed=["/tmp/inbox/core/.dead/broken.md"],
    )
    assert any("briefing-malformed" in w for w in result.warnings)


def test_warning_dois_briefings_concorrentes(db_path: Path):
    """Dois briefings iniciado pra mesmo (epic, project) sem issue → warning."""
    b1 = _briefing(created="2026-05-16T10:00:00Z", subject="task-a")
    b2 = _briefing(created="2026-05-16T10:30:00Z", subject="task-b")
    result = _run(
        db_path,
        briefings=[b1, b2],
        now_epoch=EPOCH_BRIEFING + 100,  # ambos dentro do threshold (iniciado)
        stale_threshold_s=3600,
    )
    assert any("multiple-briefings-no-issue" in w for w in result.warnings)


def test_warning_orphan_issue_sem_briefing(db_path: Path):
    """Issue com âncora mas sem briefing correspondente → warning orphan-issue."""
    # Issue tem âncora apontando pra briefing que não existe em inbox
    body = (
        "<!-- mmb-cycle-key: e1/core/2026-05-16T10:00:00Z -->\n"
        "# briefing some"
    )
    issue = GhIssue(
        repo="mmb-core",
        number=42,
        title="t",
        body=body,
        state="OPEN",
        labels=("task", "project:mmb-core", "epic:e1"),
        created_at=TS_ISSUE,
        closed_at=None,
    )
    result = _run(
        db_path,
        briefings=[],  # nenhum briefing em inbox
        issues={"mmb-core": [issue]},
    )
    assert any("orphan-issue" in w for w in result.warnings)


def test_orphan_issue_ainda_cria_ciclo(db_path: Path):
    """Mesmo com orphan-issue, ciclo é criado (fallback fase 1)."""
    body = (
        "<!-- mmb-cycle-key: e1/core/2026-05-16T10:00:00Z -->\n"
        "# x"
    )
    issue = GhIssue(
        repo="mmb-core",
        number=42,
        title="t",
        body=body,
        state="OPEN",
        labels=("task", "project:mmb-core", "epic:e1"),
        created_at=TS_ISSUE,
        closed_at=None,
    )
    _run(
        db_path,
        briefings=[],
        issues={"mmb-core": [issue]},
    )
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT id, status, planner_invoked_at, briefing_md FROM ciclos"
        ).fetchone()
        # Cycle id usa anchor.briefing_ts mesmo sem briefing
        assert row["id"] == "e1__core__2026-05-16T10:00:00Z"
        assert row["status"] == "planejado"
        # briefing_md fica NULL porque briefing não foi encontrado
        assert row["briefing_md"] is None


# ── Multi-repo / multi-projeto ─────────────────────────────────────


def test_multiprojeto_briefings_separados(db_path: Path):
    """Mesmo épico, 3 projetos: 3 ciclos independentes."""
    b_core = _briefing(epic="e1", project="core")
    b_cockpit = _briefing(epic="e1", project="cockpit")
    b_aquarium = _briefing(epic="e1", project="aquarium")

    _run(
        db_path,
        briefings=[b_core, b_cockpit, b_aquarium],
        repos=("mmb-core", "mmb-cockpit", "mmb-aquarium"),
        now_epoch=EPOCH_BRIEFING + 60,
    )
    with get_conn(db_path) as conn:
        ciclos = list(conn.execute("SELECT id, project, status FROM ciclos ORDER BY project"))
        assert len(ciclos) == 3
        assert all(c["status"] == "iniciado" for c in ciclos)
        projects = [c["project"] for c in ciclos]
        assert projects == ["mmb-aquarium", "mmb-cockpit", "mmb-core"]


# ── Sanity: abort_origin estendido aceito pelo schema ───────────────


def test_schema_aceita_abort_origin_estendido(db_path: Path):
    """CHECK constraint de ciclos.abort_origin aceita worker-exit/timeout/stale."""
    with get_conn(db_path) as conn:
        # Insere epico primeiro (FK)
        conn.execute(
            "INSERT INTO epicos (id, slug, started_at, intencao, status) "
            "VALUES ('e', 'e', '2026-01-01T00:00:00Z', 'e', 'aberto')"
        )
        for origin in ("worker-exit", "worker-timeout", "stale"):
            conn.execute(
                """
                INSERT INTO ciclos (id, epico_id, project, planner_invoked_at,
                                    status, instruction, abort_origin)
                VALUES (?, 'e', 'mmb-core', '2026-01-01T00:00:00Z',
                        'abortado', 't', ?)
                """,
                (f"cid-{origin}", origin),
            )
        n = conn.execute("SELECT COUNT(*) AS n FROM ciclos").fetchone()["n"]
        assert n == 3


def test_schema_rejeita_abort_origin_invalido(db_path: Path):
    """CHECK constraint rejeita valor fora da lista."""
    import sqlite3

    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO epicos (id, slug, started_at, intencao, status) "
            "VALUES ('e', 'e', '2026-01-01T00:00:00Z', 'e', 'aberto')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO ciclos (id, epico_id, project, planner_invoked_at,
                                    status, instruction, abort_origin)
                VALUES ('cid-bad', 'e', 'mmb-core', '2026-01-01T00:00:00Z',
                        'abortado', 't', 'unknown-origin')
                """
            )
