"""Regras de inferência de ciclo R1-R10.

Cada regra é função pura sobre uma conexão SQLite aberta: aplica
INSERT/UPDATE + insere evento conforme manual. Não abre/fecha transação;
chamador (runner) controla commit.

Heurística R6/R7 (atomic_deregister → ciclo aberto): hoje o schema do
andaime não carrega `task_id` explícito nos frontmatters, então o
match agent → ciclo cai num "último ciclo aberto do projeto". É um
trade-off conhecido — anote em revisão futura.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from typing import Any

from mmb_logger.db import (
    find_ciclo_for_transition,
    find_latest_open_ciclo_by_project,
    insert_evento,
    update_ciclo_status,
    upsert_ciclo,
    upsert_epico,
)
from mmb_logger.ingest.agents_stream import AgentEvent, decode_agent_id
from mmb_logger.ingest.inbox import InboxMessage
from mmb_logger.ingest.journal import JournalEntry

REPOS = ("core", "cockpit", "aquarium")

PR_URL_RE = re.compile(r"https://github\.com/[\w-]+/[\w-]+/pull/(\d+)")
ISSUE_CRIADA_RE = re.compile(r"^issue-criada-(\d+)$")
PR_ABERTO_RE = re.compile(r"^pr-aberto-(\d+)$")
TASK_FECHADA_RE = re.compile(r"^task-fechada-")
TASK_ABORTADA_RE = re.compile(r"^task-abortada-")


def _project_full(short: str) -> str:
    """`core` → `mmb-core`."""
    return f"mmb-{short}"


def _first_line(body: str) -> str:
    for line in body.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _ciclo_id(thread: str, to: str, created: str) -> str:
    return f"{thread}__{to}__{created}"


def _msg_payload(msg: InboxMessage, extras: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "from": msg.from_,
        "to": msg.to,
        "type": msg.type,
        "subject": msg.subject,
        "thread": msg.thread,
        "body_excerpt": msg.body[:240],
    }
    if extras:
        payload.update(extras)
    return payload


def _log_warn(text: str) -> None:
    print(f"[mmb-logger:inference] {text}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Regras R1-R5 — inbox messages
# ---------------------------------------------------------------------------


def apply_inbox_message(conn: sqlite3.Connection, msg: InboxMessage) -> None:
    """Despacha mensagem da inbox para a regra apropriada (R1-R5, R9)."""
    if msg.from_ == "master" and msg.to in REPOS and msg.type == "briefing":
        _rule_r1_briefing(conn, msg)
        return

    if msg.from_ in REPOS and msg.to == "master" and msg.type == "status":
        if ISSUE_CRIADA_RE.match(msg.subject):
            _rule_r2_issue_criada(conn, msg)
            return
        if PR_ABERTO_RE.match(msg.subject):
            _rule_r3_pr_aberto(conn, msg)
            return
        if TASK_FECHADA_RE.match(msg.subject):
            _rule_r4_task_fechada(conn, msg)
            return

    if msg.from_ in REPOS and msg.to == "master" and msg.type == "error":
        if TASK_ABORTADA_RE.match(msg.subject):
            _rule_r5_task_abortada(conn, msg)
            return

    # R9 — fallback: question/answer/qualquer outra coisa vira evento solto.
    _rule_r9_generic_message(conn, msg)


def _rule_r1_briefing(conn: sqlite3.Connection, msg: InboxMessage) -> None:
    """R1: master → repo `briefing` cria épico (se faltar) + ciclo."""
    if not msg.thread:
        # Sem thread: não dá pra ancorar em épico. Vira evento solto.
        insert_evento(
            conn,
            ciclo_id=None,
            ts=msg.created,
            kind="msg_send",
            severity="info",
            payload=_msg_payload(msg, {"note": "briefing sem thread"}),
        )
        return

    intencao = msg.subject if msg.subject else _first_line(msg.body)
    upsert_epico(
        conn,
        id=msg.thread,
        slug=msg.thread,
        started_at=msg.created,
        intencao=intencao,
        status="aberto",
    )

    cid = _ciclo_id(msg.thread, msg.to, msg.created)
    instruction = (msg.subject + "\n\n" + msg.body[:500]).strip()
    upsert_ciclo(
        conn,
        id=cid,
        epico_id=msg.thread,
        project=_project_full(msg.to),
        planner_invoked_at=msg.created,
        status="iniciado",
        instruction=instruction,
        briefing_md=msg.body,
    )
    insert_evento(
        conn,
        ciclo_id=cid,
        ts=msg.created,
        kind="msg_send",
        severity="info",
        payload=_msg_payload(msg),
    )


def _rule_r2_issue_criada(conn: sqlite3.Connection, msg: InboxMessage) -> None:
    """R2: repo → master `status: issue-criada-N` → status = planejado."""
    if not msg.thread:
        _rule_r9_generic_message(conn, msg)
        return
    m = ISSUE_CRIADA_RE.match(msg.subject)
    issue_number = int(m.group(1)) if m else None

    ciclo = find_ciclo_for_transition(
        conn,
        epico_id=msg.thread,
        project=_project_full(msg.from_),
        accepted_statuses=["iniciado"],
    )
    if not ciclo:
        _log_warn(
            f"R2: ciclo não encontrado para thread={msg.thread} "
            f"project={_project_full(msg.from_)} (subject={msg.subject})"
        )
        insert_evento(
            conn,
            ciclo_id=None,
            ts=msg.created,
            kind="msg_receive",
            severity="warn",
            payload=_msg_payload(msg, {"issue_number": issue_number, "note": "ciclo não casou"}),
        )
        return

    update_ciclo_status(conn, ciclo["id"], status="planejado")
    insert_evento(
        conn,
        ciclo_id=ciclo["id"],
        ts=msg.created,
        kind="msg_receive",
        severity="info",
        payload=_msg_payload(msg, {"issue_number": issue_number}),
    )
    insert_evento(
        conn,
        ciclo_id=ciclo["id"],
        ts=msg.created,
        kind="state_change",
        severity="info",
        payload={"from": "iniciado", "to": "planejado", "trigger": "issue-criada"},
    )


def _rule_r3_pr_aberto(conn: sqlite3.Connection, msg: InboxMessage) -> None:
    """R3: repo → master `status: pr-aberto-N` → status = pr_aberto."""
    if not msg.thread:
        _rule_r9_generic_message(conn, msg)
        return
    m = PR_ABERTO_RE.match(msg.subject)
    pr_number = int(m.group(1)) if m else None

    ciclo = find_ciclo_for_transition(
        conn,
        epico_id=msg.thread,
        project=_project_full(msg.from_),
        accepted_statuses=["planejado", "iniciado"],
    )
    if not ciclo:
        _log_warn(
            f"R3: ciclo não encontrado para thread={msg.thread} "
            f"project={_project_full(msg.from_)} (subject={msg.subject})"
        )
        insert_evento(
            conn,
            ciclo_id=None,
            ts=msg.created,
            kind="pr_opened",
            severity="warn",
            payload=_msg_payload(msg, {"pr_number": pr_number, "note": "ciclo não casou"}),
        )
        return

    pr_url = None
    url_match = PR_URL_RE.search(msg.body)
    if url_match:
        pr_url = url_match.group(0)

    update_ciclo_status(
        conn,
        ciclo["id"],
        status="pr_aberto",
        closed_partial_at=msg.created,
        pr_number=pr_number,
        pr_url=pr_url,
    )
    insert_evento(
        conn,
        ciclo_id=ciclo["id"],
        ts=msg.created,
        kind="pr_opened",
        severity="info",
        payload=_msg_payload(msg, {"pr_number": pr_number, "pr_url": pr_url}),
    )


def _rule_r4_task_fechada(conn: sqlite3.Connection, msg: InboxMessage) -> None:
    """R4: repo → master `status: task-fechada-*` → status = completo."""
    if not msg.thread:
        _rule_r9_generic_message(conn, msg)
        return
    ciclo = find_ciclo_for_transition(
        conn,
        epico_id=msg.thread,
        project=_project_full(msg.from_),
        accepted_statuses=["pr_aberto"],
    )
    if not ciclo:
        _log_warn(
            f"R4: ciclo não encontrado para thread={msg.thread} "
            f"project={_project_full(msg.from_)} (subject={msg.subject})"
        )
        insert_evento(
            conn,
            ciclo_id=None,
            ts=msg.created,
            kind="state_change",
            severity="warn",
            payload=_msg_payload(msg, {"note": "ciclo não casou em R4"}),
        )
        return

    update_ciclo_status(conn, ciclo["id"], status="completo", closed_complete_at=msg.created)
    insert_evento(
        conn,
        ciclo_id=ciclo["id"],
        ts=msg.created,
        kind="state_change",
        severity="info",
        payload={"from": "pr_aberto", "to": "completo", "trigger": "task-fechada"},
    )


def _rule_r5_task_abortada(conn: sqlite3.Connection, msg: InboxMessage) -> None:
    """R5: repo → master `error: task-abortada-*` → status = abortado/master."""
    if not msg.thread:
        _rule_r9_generic_message(conn, msg)
        return
    ciclo = find_ciclo_for_transition(
        conn,
        epico_id=msg.thread,
        project=_project_full(msg.from_),
        accepted_statuses=["iniciado", "planejado", "pr_aberto"],
    )
    reason = _first_line(msg.body) or msg.subject
    if not ciclo:
        _log_warn(
            f"R5: ciclo não encontrado para thread={msg.thread} "
            f"project={_project_full(msg.from_)} (subject={msg.subject})"
        )
        insert_evento(
            conn,
            ciclo_id=None,
            ts=msg.created,
            kind="state_change",
            severity="error",
            payload=_msg_payload(msg, {"abort_reason": reason, "note": "ciclo não casou em R5"}),
        )
        return

    update_ciclo_status(
        conn,
        ciclo["id"],
        status="abortado",
        abort_at=msg.created,
        abort_origin="master",
        abort_reason=reason,
    )
    insert_evento(
        conn,
        ciclo_id=ciclo["id"],
        ts=msg.created,
        kind="state_change",
        severity="error",
        payload={
            "to": "abortado",
            "abort_origin": "master",
            "abort_reason": reason,
            "subject": msg.subject,
        },
    )


def _rule_r9_generic_message(conn: sqlite3.Connection, msg: InboxMessage) -> None:
    """R9: mensagem que não casou com nenhuma regra de transição.

    Registra como msg_send (se from = master) ou msg_receive (se to = master).
    Tenta linkar a ciclo se thread + project baterem.
    """
    kind = "msg_send" if msg.from_ == "master" else "msg_receive"
    severity = "error" if msg.type == "error" else "info"

    ciclo_id: str | None = None
    if msg.thread:
        project_short = msg.to if msg.from_ == "master" else msg.from_
        if project_short in REPOS:
            ciclo = find_ciclo_for_transition(
                conn,
                epico_id=msg.thread,
                project=_project_full(project_short),
                accepted_statuses=["iniciado", "planejado", "pr_aberto", "completo", "abortado"],
            )
            if ciclo:
                ciclo_id = ciclo["id"]

    insert_evento(
        conn,
        ciclo_id=ciclo_id,
        ts=msg.created,
        kind=kind,
        severity=severity,
        payload=_msg_payload(msg),
    )


# ---------------------------------------------------------------------------
# Regras R6-R7 — agents.jsonl
# ---------------------------------------------------------------------------


def _classify_abort_origin(reason: str | None) -> str | None:
    if not reason:
        return None
    r = reason.lower()
    if "heartbeat" in r or "timeout" in r:
        return "heartbeat"
    if r.startswith("self") or "self-" in r:
        return "self"
    if "demo-end" in r or "manual" in r:
        return "manual"
    return None


def apply_agent_event(conn: sqlite3.Connection, evt: AgentEvent) -> None:
    """Despacha eventos de `agents.jsonl` para R6 (deregister) ou R7 (spawn)."""
    if evt.ev == "spawn":
        _rule_r7_spawn(conn, evt)
        return
    if evt.ev == "deregister":
        _rule_r6_deregister(conn, evt)
        return
    # heartbeat e outros: ignorar por enquanto (volume alto, valor baixo).


def _rule_r7_spawn(conn: sqlite3.Connection, evt: AgentEvent) -> None:
    """R7: spawn de atômico. Registra evento, tenta linkar a ciclo aberto."""
    project_short, task_id = decode_agent_id(evt.id)
    ciclo_id: str | None = None
    if project_short:
        # Heurística frouxa: último ciclo aberto do projeto. Trade-off
        # conhecido enquanto schema do andaime não carrega task_id explícito.
        ciclo = find_latest_open_ciclo_by_project(conn, project=_project_full(project_short))
        if ciclo:
            ciclo_id = ciclo["id"]
    insert_evento(
        conn,
        ciclo_id=ciclo_id,
        ts=evt.ts,
        kind="atomic_spawn",
        severity="info",
        payload={
            "agent_id": evt.id,
            "parent": evt.parent,
            "pane": evt.pane,
            "pid": evt.pid,
            "task": task_id,
            "epic": evt.epic,
        },
    )


def _rule_r6_deregister(conn: sqlite3.Connection, evt: AgentEvent) -> None:
    """R6: deregister. Pode marcar ciclo como abortado se origem for automática."""
    project_short, task_id = decode_agent_id(evt.id)
    ciclo_id: str | None = None
    ciclo: dict[str, Any] | None = None
    if project_short:
        # Mesma heurística do R7 — match frouxo por projeto sem task_id explícito.
        ciclo = find_latest_open_ciclo_by_project(conn, project=_project_full(project_short))
        if ciclo:
            ciclo_id = ciclo["id"]

    origin = _classify_abort_origin(evt.reason)
    # Se ciclo já completou (PR mergeado, atômico saiu naturalmente): não aborta.
    if ciclo and ciclo["status"] not in ("completo", "abortado") and origin is not None:
        update_ciclo_status(
            conn,
            ciclo["id"],
            status="abortado",
            abort_at=evt.ts,
            abort_origin=origin,
            abort_reason=evt.reason or "deregister sem reason",
        )
        insert_evento(
            conn,
            ciclo_id=ciclo["id"],
            ts=evt.ts,
            kind="state_change",
            severity="error",
            payload={
                "to": "abortado",
                "abort_origin": origin,
                "abort_reason": evt.reason,
                "agent_id": evt.id,
            },
        )

    insert_evento(
        conn,
        ciclo_id=ciclo_id,
        ts=evt.ts,
        kind="atomic_deregister",
        severity="info",
        payload={
            "agent_id": evt.id,
            "reason": evt.reason,
            "task": task_id,
            "epic": evt.epic,
            "pid": evt.pid,
        },
    )


# ---------------------------------------------------------------------------
# Regra R8 — journal.jsonl
# ---------------------------------------------------------------------------


def apply_journal_entry(conn: sqlite3.Connection, entry: JournalEntry) -> None:
    """R8: linhas do journal viram eventos `journal_<sev>`. Linka se epic/task baterem."""
    sev = entry.sev if entry.sev in ("warn", "error", "critical") else "warn"
    kind = f"journal_{sev}"
    ciclo_id: str | None = None
    if entry.epic:
        # Heurística: pega último ciclo aberto do épico (qualquer projeto).
        row = conn.execute(
            """
            SELECT * FROM ciclos
            WHERE epico_id = ? AND status IN ('iniciado', 'planejado', 'pr_aberto')
            ORDER BY planner_invoked_at DESC
            LIMIT 1
            """,
            (entry.epic,),
        ).fetchone()
        if row:
            ciclo_id = row["id"]
    insert_evento(
        conn,
        ciclo_id=ciclo_id,
        ts=entry.ts,
        kind=kind,
        severity=sev,
        payload=entry.raw,
    )
