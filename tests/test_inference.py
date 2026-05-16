"""Cobertura das regras R1-R10."""

from __future__ import annotations

import sqlite3

from mmb_logger.db import get_ciclo, get_epico, list_eventos_by_ciclo
from mmb_logger.ingest.agents_stream import AgentEvent
from mmb_logger.ingest.inbox import InboxMessage
from mmb_logger.ingest.inference import (
    apply_agent_event,
    apply_inbox_message,
    apply_journal_entry,
)
from mmb_logger.ingest.journal import JournalEntry


def _msg(**kw) -> InboxMessage:
    defaults = {
        "path": "/tmp/x.md",
        "from_": "master",
        "to": "cockpit",
        "type": "briefing",
        "subject": "x",
        "thread": "ep1",
        "created": "2026-05-10T10:00:00Z",
        "body": "",
    }
    defaults.update(kw)
    return InboxMessage(**defaults)


def _apply_briefing(conn: sqlite3.Connection) -> str:
    """Helper: aplica R1 e devolve ciclo_id."""
    msg = _msg(type="briefing", subject="M2", body="briefing body")
    apply_inbox_message(conn, msg)
    return f"ep1__cockpit__{msg.created}"


# ---------------------------------------------------------------------------
# R1
# ---------------------------------------------------------------------------


def test_r1_briefing_cria_epico_e_ciclo(conn: sqlite3.Connection):
    cid = _apply_briefing(conn)
    ep = get_epico(conn, "ep1")
    assert ep is not None
    assert ep["status"] == "aberto"
    c = get_ciclo(conn, cid)
    assert c is not None
    assert c["project"] == "mmb-cockpit"
    assert c["status"] == "iniciado"
    assert c["briefing_md"] == "briefing body"


def test_r1_briefing_sem_thread_vira_evento_solto(conn: sqlite3.Connection):
    msg = _msg(thread=None)
    apply_inbox_message(conn, msg)
    # Ciclo não foi criado.
    row = conn.execute("SELECT COUNT(*) AS n FROM ciclos").fetchone()
    assert row["n"] == 0
    # Mas evento sim.
    row = conn.execute("SELECT COUNT(*) AS n FROM eventos").fetchone()
    assert row["n"] == 1


def test_r1_idempotente(conn: sqlite3.Connection):
    _apply_briefing(conn)
    _apply_briefing(conn)
    row = conn.execute("SELECT COUNT(*) AS n FROM ciclos").fetchone()
    assert row["n"] == 1


# ---------------------------------------------------------------------------
# R2
# ---------------------------------------------------------------------------


def test_r2_issue_criada(conn: sqlite3.Connection):
    cid = _apply_briefing(conn)
    apply_inbox_message(
        conn,
        _msg(
            from_="cockpit",
            to="master",
            type="status",
            subject="issue-criada-42",
            created="2026-05-10T10:30:00Z",
        ),
    )
    c = get_ciclo(conn, cid)
    assert c["status"] == "planejado"


# ---------------------------------------------------------------------------
# R3
# ---------------------------------------------------------------------------


def test_r3_pr_aberto(conn: sqlite3.Connection):
    cid = _apply_briefing(conn)
    apply_inbox_message(
        conn,
        _msg(
            from_="cockpit",
            to="master",
            type="status",
            subject="issue-criada-42",
            created="2026-05-10T10:30:00Z",
        ),
    )
    apply_inbox_message(
        conn,
        _msg(
            from_="cockpit",
            to="master",
            type="status",
            subject="pr-aberto-43",
            created="2026-05-10T11:00:00Z",
            body="PR: https://github.com/x-force-42/mmb-cockpit/pull/43",
        ),
    )
    c = get_ciclo(conn, cid)
    assert c["status"] == "pr_aberto"
    assert c["pr_number"] == 43
    assert c["pr_url"] == "https://github.com/x-force-42/mmb-cockpit/pull/43"
    assert c["closed_partial_at"] == "2026-05-10T11:00:00Z"


# ---------------------------------------------------------------------------
# R4
# ---------------------------------------------------------------------------


def test_r4_task_fechada(conn: sqlite3.Connection):
    cid = _apply_briefing(conn)
    apply_inbox_message(
        conn,
        _msg(
            from_="cockpit",
            to="master",
            type="status",
            subject="issue-criada-42",
            created="2026-05-10T10:30:00Z",
        ),
    )
    apply_inbox_message(
        conn,
        _msg(
            from_="cockpit",
            to="master",
            type="status",
            subject="pr-aberto-43",
            created="2026-05-10T11:00:00Z",
        ),
    )
    apply_inbox_message(
        conn,
        _msg(
            from_="cockpit",
            to="master",
            type="status",
            subject="task-fechada-M2",
            created="2026-05-10T12:00:00Z",
        ),
    )
    c = get_ciclo(conn, cid)
    assert c["status"] == "completo"
    assert c["closed_complete_at"] == "2026-05-10T12:00:00Z"


# ---------------------------------------------------------------------------
# R5
# ---------------------------------------------------------------------------


def test_r5_task_abortada(conn: sqlite3.Connection):
    cid = _apply_briefing(conn)
    apply_inbox_message(
        conn,
        _msg(
            from_="cockpit",
            to="master",
            type="error",
            subject="task-abortada-X1",
            body="dependência não satisfeita",
            created="2026-05-10T11:30:00Z",
        ),
    )
    c = get_ciclo(conn, cid)
    assert c["status"] == "abortado"
    assert c["abort_origin"] == "master"
    assert "dependência" in c["abort_reason"]


# ---------------------------------------------------------------------------
# R6
# ---------------------------------------------------------------------------


def test_r6_deregister_heartbeat_marca_abortado(conn: sqlite3.Connection):
    cid = _apply_briefing(conn)
    evt = AgentEvent(
        ts="2026-05-10T13:00:00Z",
        ev="deregister",
        id="cockpit-M2",
        parent=None,
        pane=None,
        pid=123,
        reason="heartbeat-timeout",
        task="M2",
        epic="ep1",
        raw={},
    )
    apply_agent_event(conn, evt)
    c = get_ciclo(conn, cid)
    assert c["status"] == "abortado"
    assert c["abort_origin"] == "heartbeat"


def test_r6_deregister_de_ciclo_completo_nao_aborta(conn: sqlite3.Connection):
    cid = _apply_briefing(conn)
    # Promove a completo primeiro.
    conn.execute("UPDATE ciclos SET status='completo' WHERE id = ?", (cid,))
    evt = AgentEvent(
        ts="2026-05-10T13:00:00Z",
        ev="deregister",
        id="cockpit-M2",
        parent=None,
        pane=None,
        pid=123,
        reason="self-done",
        task="M2",
        epic="ep1",
        raw={},
    )
    apply_agent_event(conn, evt)
    c = get_ciclo(conn, cid)
    assert c["status"] == "completo"


# ---------------------------------------------------------------------------
# R7
# ---------------------------------------------------------------------------


def test_r7_spawn_gera_evento(conn: sqlite3.Connection):
    cid = _apply_briefing(conn)
    evt = AgentEvent(
        ts="2026-05-10T10:05:00Z",
        ev="spawn",
        id="cockpit-M2",
        parent="cockpit",
        pane="%42",
        pid=999,
        reason=None,
        task="M2",
        epic="ep1",
        raw={},
    )
    apply_agent_event(conn, evt)
    eventos = list_eventos_by_ciclo(conn, cid)
    kinds = [e["kind"] for e in eventos]
    assert "atomic_spawn" in kinds


# ---------------------------------------------------------------------------
# R8
# ---------------------------------------------------------------------------


def test_r8_journal_warn_gera_evento_e_linka(conn: sqlite3.Connection):
    cid = _apply_briefing(conn)
    entry = JournalEntry(
        ts="2026-05-10T10:10:00Z",
        sev="warn",
        ev="slow-spawn",
        msg="lento",
        epic="ep1",
        task="M2",
        resolves=None,
        raw={"ts": "x", "sev": "warn", "ev": "slow-spawn", "msg": "lento", "epic": "ep1"},
    )
    apply_journal_entry(conn, entry)
    eventos = list_eventos_by_ciclo(conn, cid)
    kinds = [e["kind"] for e in eventos]
    assert "journal_warn" in kinds


# ---------------------------------------------------------------------------
# R9
# ---------------------------------------------------------------------------


def test_r9_question_so_registra_evento(conn: sqlite3.Connection):
    cid = _apply_briefing(conn)
    apply_inbox_message(
        conn,
        _msg(
            from_="cockpit",
            to="master",
            type="question",
            subject="duvida-X",
            created="2026-05-10T10:20:00Z",
        ),
    )
    c = get_ciclo(conn, cid)
    # Status não mudou.
    assert c["status"] == "iniciado"
    eventos = list_eventos_by_ciclo(conn, cid)
    assert any(e["kind"] == "msg_receive" for e in eventos)


# ---------------------------------------------------------------------------
# R10 — lifecycle subdirs no `ingest-once`
# ---------------------------------------------------------------------------


def test_r10_lifecycle_subdirs_sao_lidos_no_ingest_once(tmp_path, db_path):
    """Garante que `.done/` e raiz contam como paths diferentes,
    `mark_file_processed` impede duplicação, e ambos os arquivos
    geram apenas 1 evento (porque o mesmo conteúdo só é processado
    uma vez por path único)."""
    from mmb_logger.ingest.runner import ingest_inbox_files

    base = tmp_path / "inbox" / "master"
    base.mkdir(parents=True)
    body = (
        "---\nfrom: master\nto: cockpit\ntype: briefing\nsubject: x\n"
        "thread: ep1\ncreated: 2026-05-10T10:00:00Z\n---\n"
    )
    (base / "vivo.md").write_text(body)
    (base / ".done").mkdir()
    (base / ".done" / "antigo.md").write_text(body)

    from mmb_logger.db import get_conn

    with get_conn(db_path) as conn:
        novos, total = ingest_inbox_files(conn, tmp_path)
    assert total == 2
    assert novos == 2
    # Idempotência: segunda chamada não adiciona ciclos.
    with get_conn(db_path) as conn:
        novos2, _ = ingest_inbox_files(conn, tmp_path)
    assert novos2 == 0
