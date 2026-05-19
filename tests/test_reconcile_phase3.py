"""Testes da fase 3 do reconcile: audit (journal/agents/inbox) + intents.

Cada teste cria um `.tooling/` mínimo em tmp_path e injeta dados via
arquivos reais. Garante:
- audit gera eventos com source_key, INSERT OR IGNORE → idempotência.
- linkagem a ciclo via heurísticas claras; órfãos permitidos com warning.
- R1-R5 mortos: subject `pr-aberto-N` NÃO transiciona ciclo.
- epicos.intencao preenchida a partir de master-briefing.md.
- assertiveness_score / review_note continuam preservados.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from mmb_logger.db import get_conn, patch_ciclo
from mmb_logger.reconcile.reconcile import reconcile

# ── Fixture: .tooling/ mínimo ─────────────────────────────────────


@pytest.fixture()
def tooling(tmp_path: Path) -> Path:
    """Cria estrutura de `.tooling/` vazia. Testes preenchem o que precisarem."""
    root = tmp_path / "tooling"
    (root / "inbox" / "master").mkdir(parents=True)
    (root / "inbox" / "core").mkdir(parents=True)
    (root / "inbox" / "cockpit").mkdir(parents=True)
    (root / "inbox" / "aquarium").mkdir(parents=True)
    (root / "logs").mkdir(parents=True)
    (root / "state").mkdir(parents=True)
    (root / "intents").mkdir(parents=True)
    return root


def _write_journal(tooling: Path, lines: list[dict]) -> None:
    path = tooling / "logs" / "journal.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for d in lines:
            f.write(json.dumps(d) + "\n")


def _write_agents(tooling: Path, lines: list[dict]) -> None:
    path = tooling / "state" / "agents.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for d in lines:
            f.write(json.dumps(d) + "\n")


def _write_inbox_msg(
    tooling: Path,
    *,
    dest: str,
    from_: str,
    type_: str,
    subject: str,
    thread: str | None,
    created: str,
    body: str = "body",
) -> Path:
    """Cria arquivo de inbox válido com frontmatter."""
    name = f"{created.replace(':', '-')}_{from_}_{type_}_{subject}.md"
    path = tooling / "inbox" / dest / name
    fm = [
        "---",
        f"from: {from_}",
        f"to: {dest}",
        f"type: {type_}",
        f"subject: {subject}",
    ]
    if thread:
        fm.append(f"thread: {thread}")
    fm.append(f"created: {created}")
    fm.append("---")
    fm.append("")
    fm.append(body)
    path.write_text("\n".join(fm), encoding="utf-8")
    return path


def _write_master_briefing(
    tooling: Path,
    *,
    epic_slug: str,
    date_prefix: str = "2026-05-16",
    intent_h1: str = "Test intent — preencher epicos.intencao",
    body_extra: str = "\n## Detalhes\n\nblá blá\n",
) -> Path:
    """Cria .tooling/intents/<date>-<slug>/master-briefing.md."""
    dir_ = tooling / "intents" / f"{date_prefix}-{epic_slug}"
    dir_.mkdir(parents=True, exist_ok=True)
    path = dir_ / "master-briefing.md"
    path.write_text(f"# {intent_h1}\n{body_extra}", encoding="utf-8")
    return path


def _run(
    db_path: Path,
    tooling: Path,
    *,
    issues=None,
    prs=None,
    briefings=None,
    journal_signals=None,
    agent_signals=None,
    repos=("mmb-core",),
    now_epoch: float | None = None,
    stale_threshold_s: int = 3600,
):
    """Helper genérico. tooling apontado pro fixture; audit/intents leem dele."""

    def fi(_owner, repo, **_kw):
        return (issues or {}).get(repo, [])

    def fp(_owner, repo, **_kw):
        return (prs or {}).get(repo, [])

    if now_epoch is None:
        now_epoch = datetime.fromisoformat("2026-05-16T20:00:00+00:00").timestamp()
    return reconcile(
        db_path=str(db_path),
        fetch_issues_fn=fi,
        fetch_prs_fn=fp,
        repos=repos,
        andaime_version_fn=lambda: None,
        tooling_root=str(tooling),
        claude_projects_root=str(tooling.parent),  # transcripts no-op
        briefings=briefings,  # None → carrega do tooling fixture
        # journal/agent signals ainda injetáveis se teste quiser explicar
        journal_signals=journal_signals,
        agent_signals=agent_signals,
        now_epoch=now_epoch,
        stale_threshold_s=stale_threshold_s,
        targets_for_sync=[],
    )


# ── Audit: journal events ─────────────────────────────────────────


def test_journal_event_com_epic_linka_a_ciclo(db_path: Path, tooling: Path):
    """journal warn/error/critical com epic+task casável → evento linkado."""
    # Primeiro cria ciclo via briefing pra dar matching pra journal entry
    _write_inbox_msg(
        tooling,
        dest="core",
        from_="master",
        type_="briefing",
        subject="task-x",
        thread="my-epic",
        created="2026-05-16T10:00:00Z",
        body="# briefing",
    )
    # Journal entry referenciando esse épico (log.sh format)
    _write_journal(
        tooling,
        [
            {
                "ts": "2026-05-16T10:30:00Z",
                "sev": "error",
                "event": "hook-failed",
                "msg": "alguma coisa explodiu",
                "epic": "my-epic",
                "task": "core-X1",
                "agent": "core-X1",
            }
        ],
    )
    _run(
        db_path, tooling,
        now_epoch=datetime.fromisoformat("2026-05-16T10:35:00+00:00").timestamp(),
        stale_threshold_s=3600,
    )
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT ciclo_id, kind, severity FROM eventos WHERE kind LIKE 'journal_%'"
        ).fetchone()
        assert row is not None
        assert row["kind"] == "journal_error"
        assert row["severity"] == "error"
        assert row["ciclo_id"] is not None  # casou via epic+project


def test_journal_event_sem_identificacao_vira_orfao(db_path: Path, tooling: Path):
    """journal sem epic nem task → evento com ciclo_id NULL + warning orphan."""
    _write_journal(
        tooling,
        [
            {
                "ts": "2026-05-16T10:30:00Z",
                "sev": "warn",
                "event": "foo-bar",
                "msg": "sem contexto",
            }
        ],
    )
    result = _run(db_path, tooling)
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT ciclo_id, kind FROM eventos WHERE kind='journal_warn'"
        ).fetchone()
        assert row is not None
        assert row["ciclo_id"] is None
    assert any("journal-event-orphan" in w for w in result.warnings)


def test_journal_commd_event_nao_vira_audit(db_path: Path, tooling: Path):
    """commd-* (info, sem sev) NÃO entra como journal_info — só warn/error/critical."""
    _write_journal(
        tooling,
        [
            {
                "ts": "2026-05-16T10:30:00Z",
                "event": "commd-dispatch",
                "dest": "core",
                "file": "x.md",
                "pid": 1,
            }
        ],
    )
    _run(db_path, tooling)
    with get_conn(db_path) as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM eventos WHERE kind LIKE 'journal_%'"
        ).fetchone()["n"]
        assert n == 0


# ── Audit: agents events ──────────────────────────────────────────


def test_agents_spawn_e_deregister_viram_eventos(db_path: Path, tooling: Path):
    """spawn/deregister geram atomic_spawn / atomic_deregister."""
    _write_agents(
        tooling,
        [
            {
                "ts": "2026-05-16T10:00:00Z",
                "ev": "spawn",
                "id": "core-X1",
                "parent": "core",
                "pane": "p1",
                "pid": 100,
                "task": "X1",
                "epic": "my-epic",
            },
            {
                "ts": "2026-05-16T10:15:00Z",
                "ev": "deregister",
                "id": "core-X1",
                "reason": "pr-opened",
            },
        ],
    )
    _run(db_path, tooling)
    with get_conn(db_path) as conn:
        kinds = [
            r["kind"]
            for r in conn.execute(
                "SELECT kind FROM eventos WHERE kind LIKE 'atomic_%' ORDER BY ts"
            )
        ]
        assert kinds == ["atomic_spawn", "atomic_deregister"]


def test_agents_deregister_nao_transiciona_status(db_path: Path, tooling: Path):
    """deregister com reason `pr-opened` (benigna) NÃO altera ciclo status."""
    # Cria ciclo iniciado via briefing
    _write_inbox_msg(
        tooling,
        dest="core",
        from_="master",
        type_="briefing",
        subject="x",
        thread="my-epic",
        created="2026-05-16T10:00:00Z",
    )
    _write_agents(
        tooling,
        [
            {
                "ts": "2026-05-16T10:30:00Z",
                "ev": "deregister",
                "id": "core-X1",
                "reason": "pr-opened",
                "epic": "my-epic",
            },
        ],
    )
    _run(
        db_path, tooling,
        now_epoch=datetime.fromisoformat("2026-05-16T10:35:00+00:00").timestamp(),
        stale_threshold_s=3600,
    )
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT status FROM ciclos").fetchone()
        assert row["status"] == "iniciado"  # pr-opened é benigno, fica iniciado


# ── Audit: inbox sem alterar status ───────────────────────────────


def test_inbox_briefing_vira_audit_event(db_path: Path, tooling: Path):
    """Briefing master→planner gera msg_send sem alterar status."""
    _write_inbox_msg(
        tooling,
        dest="core",
        from_="master",
        type_="briefing",
        subject="task-x",
        thread="my-epic",
        created="2026-05-16T10:00:00Z",
    )
    _run(
        db_path, tooling,
        now_epoch=datetime.fromisoformat("2026-05-16T10:30:00+00:00").timestamp(),
        stale_threshold_s=3600,
    )
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT kind FROM eventos WHERE kind='msg_send'").fetchone()
        assert row is not None


def test_inbox_subject_pr_aberto_nao_transiciona(db_path: Path, tooling: Path):
    """Subject `pr-aberto-3` NÃO leva ciclo para pr_aberto (R1-R5 mortos).

    Garantia central: o regex em subject que existia em inference.py R3 não
    transiciona mais o ciclo. Status só pode ser pr_aberto se houver
    issue+PR no GH (fase 1).
    """
    # Ciclo nasce via briefing (status iniciado)
    _write_inbox_msg(
        tooling,
        dest="core",
        from_="master",
        type_="briefing",
        subject="task-y",
        thread="my-epic",
        created="2026-05-16T10:00:00Z",
    )
    # Mensagem antiga estilo R3 — orq → master "pr-aberto-3" (subject que
    # antes disparava update_ciclo_status('pr_aberto'))
    _write_inbox_msg(
        tooling,
        dest="master",
        from_="core",
        type_="status",
        subject="pr-aberto-3",
        thread="my-epic",
        created="2026-05-16T10:10:00Z",
        body="PR aberto: https://github.com/x/r/pull/3",
    )
    # now logo após — briefing dentro do threshold, deve ficar iniciado.
    _run(
        db_path, tooling,
        now_epoch=datetime.fromisoformat("2026-05-16T10:20:00+00:00").timestamp(),
        stale_threshold_s=3600,
    )
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT status FROM ciclos").fetchone()
        # Garantia primária: subject pr-aberto-3 NÃO transicionou pra pr_aberto.
        assert row["status"] != "pr_aberto", (
            f"R1-R5 morto: subject pr-aberto-3 NÃO deveria transicionar pra "
            f"pr_aberto, mas status virou {row['status']}"
        )
        # E o estado correto vem do briefing+GH: sem issue/PR, fica iniciado.
        assert row["status"] == "iniciado"
        # Garantia secundária: audit capturou a mensagem mesmo assim.
        n_audit = conn.execute(
            "SELECT count(*) AS n FROM eventos WHERE kind='msg_receive'"
        ).fetchone()["n"]
        assert n_audit >= 1


# ── epicos.intencao via intents/ ──────────────────────────────────


def test_epicos_intencao_preenchida_de_master_briefing(db_path: Path, tooling: Path):
    """intencao deixa de ser slug placeholder após reconcile ler master-briefing.md."""
    _write_master_briefing(
        tooling,
        epic_slug="meu-epico",
        intent_h1="Reescrever pipeline de ingest pra reconcile",
    )
    # Cria briefing pra materializar o épico
    _write_inbox_msg(
        tooling,
        dest="core",
        from_="master",
        type_="briefing",
        subject="x",
        thread="meu-epico",
        created="2026-05-16T10:00:00Z",
    )
    _run(
        db_path, tooling,
        now_epoch=datetime.fromisoformat("2026-05-16T10:30:00+00:00").timestamp(),
        stale_threshold_s=3600,
    )
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT intencao FROM epicos WHERE id='meu-epico'").fetchone()
        assert row is not None
        assert row["intencao"] == "Reescrever pipeline de ingest pra reconcile"


def test_epicos_intencao_intent_sem_master_briefing_fica_slug(
    db_path: Path, tooling: Path
):
    """Sem intents/<date>-<slug>/master-briefing.md, intencao continua slug placeholder."""
    _write_inbox_msg(
        tooling,
        dest="core",
        from_="master",
        type_="briefing",
        subject="x",
        thread="epico-sem-intent",
        created="2026-05-16T10:00:00Z",
    )
    _run(
        db_path, tooling,
        now_epoch=datetime.fromisoformat("2026-05-16T10:30:00+00:00").timestamp(),
        stale_threshold_s=3600,
    )
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT intencao FROM epicos WHERE id='epico-sem-intent'"
        ).fetchone()
        assert row["intencao"] == "epico-sem-intent"  # placeholder


# ── Idempotência ──────────────────────────────────────────────────


def test_audit_idempotente_sem_duplicar(db_path: Path, tooling: Path):
    """Rodar reconcile duas vezes não duplica eventos audit."""
    _write_journal(
        tooling,
        [
            {
                "ts": "2026-05-16T10:30:00Z",
                "sev": "warn",
                "event": "x",
                "msg": "y",
            }
        ],
    )
    _write_agents(
        tooling,
        [
            {
                "ts": "2026-05-16T10:00:00Z",
                "ev": "spawn",
                "id": "core-X1",
                "epic": "e",
                "task": "X1",
            }
        ],
    )
    _write_inbox_msg(
        tooling,
        dest="core",
        from_="master",
        type_="briefing",
        subject="x",
        thread="e",
        created="2026-05-16T10:00:00Z",
    )
    _run(db_path, tooling)
    with get_conn(db_path) as conn:
        n1 = conn.execute("SELECT count(*) AS n FROM eventos").fetchone()["n"]

    _run(db_path, tooling)
    with get_conn(db_path) as conn:
        n2 = conn.execute("SELECT count(*) AS n FROM eventos").fetchone()["n"]

    assert n1 == n2, f"audit não idempotente: {n1} → {n2}"


def test_audit_preserva_campos_humanos(db_path: Path, tooling: Path):
    """assertiveness_score + review_note continuam preservados após audit run."""
    _write_inbox_msg(
        tooling,
        dest="core",
        from_="master",
        type_="briefing",
        subject="x",
        thread="e",
        created="2026-05-16T10:00:00Z",
    )
    _run(
        db_path, tooling,
        now_epoch=datetime.fromisoformat("2026-05-16T10:30:00+00:00").timestamp(),
        stale_threshold_s=3600,
    )
    with get_conn(db_path) as conn:
        cid = conn.execute("SELECT id FROM ciclos").fetchone()["id"]
        patch_ciclo(conn, cid, assertiveness_score=5, review_note="nice")

    # Adiciona audit data e re-roda
    _write_journal(
        tooling,
        [
            {
                "ts": "2026-05-16T10:30:00Z",
                "sev": "error",
                "event": "x",
                "msg": "y",
                "epic": "e",
            }
        ],
    )
    _run(
        db_path, tooling,
        now_epoch=datetime.fromisoformat("2026-05-16T10:35:00+00:00").timestamp(),
        stale_threshold_s=3600,
    )
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT assertiveness_score, review_note FROM ciclos"
        ).fetchone()
        assert row["assertiveness_score"] == 5
        assert row["review_note"] == "nice"


# ── Source-key + INSERT OR IGNORE plumbing ────────────────────────


def test_source_key_unique_index_funciona(db_path: Path):
    """UNIQUE INDEX em source_key rejeita duplicado direto."""
    import sqlite3

    with get_conn(db_path) as conn:
        # Insere epico+ciclo de teste
        conn.execute(
            "INSERT INTO epicos (id, slug, started_at, intencao, status) "
            "VALUES ('e', 'e', '2026-01-01T00:00:00Z', 'e', 'aberto')"
        )
        conn.execute(
            "INSERT INTO ciclos (id, epico_id, project, planner_invoked_at, status, instruction) "
            "VALUES ('c', 'e', 'mmb-core', '2026-01-01T00:00:00Z', 'iniciado', 'i')"
        )
        conn.execute(
            "INSERT INTO eventos (ciclo_id, ts, kind, payload_json, source_key) "
            "VALUES ('c', 't', 'msg_send', '{}', 'mykey')"
        )
        # Segundo INSERT com mesma source_key → erro
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO eventos (ciclo_id, ts, kind, payload_json, source_key) "
                "VALUES ('c', 't2', 'msg_receive', '{}', 'mykey')"
            )


def test_source_key_null_permite_multiplos(db_path: Path):
    """source_key NULL não dispara unique check — multiplos legacy convivem."""
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO epicos (id, slug, started_at, intencao, status) "
            "VALUES ('e', 'e', '2026-01-01T00:00:00Z', 'e', 'aberto')"
        )
        conn.execute(
            "INSERT INTO ciclos (id, epico_id, project, planner_invoked_at, status, instruction) "
            "VALUES ('c', 'e', 'mmb-core', '2026-01-01T00:00:00Z', 'iniciado', 'i')"
        )
        # Duas rows com source_key NULL — não deve dar erro
        conn.execute(
            "INSERT INTO eventos (ciclo_id, ts, kind, payload_json) "
            "VALUES ('c', 't1', 'msg_send', '{}')"
        )
        conn.execute(
            "INSERT INTO eventos (ciclo_id, ts, kind, payload_json) "
            "VALUES ('c', 't2', 'msg_send', '{}')"
        )
        n = conn.execute("SELECT count(*) AS n FROM eventos").fetchone()["n"]
        assert n == 2
