"""Audit writers: inbox / journal / agents → eventos.

Cada writer:
1. Lê o artefato canônico (inbox/, logs/journal.jsonl, state/agents.jsonl).
2. Constrói `source_key` único por entrada (path/ts/id).
3. INSERT OR IGNORE em `eventos` — dedup por UNIQUE INDEX parcial.
4. Tenta linkar a ciclo via heurísticas claras; permite NULL.
5. **NÃO transiciona ciclo** — eventos são audit/enriquecimento, não motor
   de estado. (Aborto pré-GH continua sendo deduzido em `abort.py` a
   partir dos mesmos arquivos mas com semântica diferente.)
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from mmb_logger.ingest.agents_stream import decode_agent_id
from mmb_logger.ingest.agents_stream import parse_line as parse_agent_line
from mmb_logger.ingest.inbox import parse_inbox_file
from mmb_logger.ingest.journal import parse_line as parse_journal_line


@dataclass
class AuditCounts:
    journal_inserted: int = 0
    agents_inserted: int = 0
    inbox_inserted: int = 0
    orphan_no_cycle: int = 0


def write_audit_events(
    conn: sqlite3.Connection,
    tooling_root: Path,
    warn,
) -> AuditCounts:
    """Roda os 3 writers e retorna contagens. `warn(msg)` é o canal de warning."""
    counts = AuditCounts()
    counts.journal_inserted, journal_orphans = _write_journal_events(
        conn, tooling_root, warn
    )
    counts.agents_inserted, agents_orphans = _write_agents_events(
        conn, tooling_root, warn
    )
    counts.inbox_inserted, inbox_orphans = _write_inbox_events(
        conn, tooling_root, warn
    )
    counts.orphan_no_cycle = journal_orphans + agents_orphans + inbox_orphans
    return counts


# ── Writers ────────────────────────────────────────────────────────


def _write_journal_events(
    conn: sqlite3.Connection,
    tooling_root: Path,
    warn,
) -> tuple[int, int]:
    """Eventos `journal_<sev>` apenas para sev ∈ {warn, error, critical}.

    Eventos commd-* (info-level, sem `sev`) são consumidos por `abort.py`
    pra detecção de worker-exit/timeout; NÃO viram audit eventos.
    """
    path = Path(tooling_root) / "logs" / "journal.jsonl"
    if not path.is_file():
        return (0, 0)
    inserted = 0
    orphans = 0
    for line in _iter_lines(path):
        entry = parse_journal_line(line)
        if entry is None:
            continue
        # Só warn/error/critical viram audit; info (commd ops) sai daqui.
        if entry.sev not in ("warn", "error", "critical"):
            continue
        kind = f"journal_{entry.sev}"
        source_key = f"journal:{entry.ts}:{entry.ev}"
        ciclo_id = _find_ciclo_for_journal(conn, entry.epic, entry.task)
        if ciclo_id is None:
            orphans += 1
            warn(
                f"journal-event-orphan: ts={entry.ts} ev={entry.ev} "
                f"epic={entry.epic} task={entry.task} sem ciclo casado"
            )
        if _insert_evento_ignore(
            conn,
            source_key=source_key,
            ciclo_id=ciclo_id,
            ts=entry.ts,
            kind=kind,
            severity=entry.sev,
            payload=entry.raw,
        ):
            inserted += 1
    return (inserted, orphans)


def _write_agents_events(
    conn: sqlite3.Connection,
    tooling_root: Path,
    warn,
) -> tuple[int, int]:
    """Eventos atomic_spawn / atomic_deregister como auditoria.

    NÃO transiciona ciclo — aborto pré-GH em `abort.py` consome o mesmo
    arquivo pelo lado de classificação semântica.
    """
    path = Path(tooling_root) / "state" / "agents.jsonl"
    if not path.is_file():
        return (0, 0)
    inserted = 0
    orphans = 0
    for line in _iter_lines(path):
        evt = parse_agent_line(line)
        if evt is None:
            continue
        if evt.ev not in ("spawn", "deregister"):
            continue  # heartbeat: volume alto, valor baixo
        kind = f"atomic_{evt.ev}"
        source_key = f"agents:{evt.id}:{evt.ts}:{evt.ev}"
        project_short, _task_id = decode_agent_id(evt.id)
        ciclo_id = None
        if project_short and evt.epic:
            ciclo_id = _find_ciclo_by_epic_project(conn, evt.epic, project_short)
        if ciclo_id is None:
            orphans += 1
        if _insert_evento_ignore(
            conn,
            source_key=source_key,
            ciclo_id=ciclo_id,
            ts=evt.ts,
            kind=kind,
            severity="info",
            payload=evt.raw,
        ):
            inserted += 1
    return (inserted, orphans)


def _write_inbox_events(
    conn: sqlite3.Connection,
    tooling_root: Path,
    warn,
) -> tuple[int, int]:
    """Toda mensagem em inbox/** vira `msg_send` ou `msg_receive` — audit puro.

    NÃO usa subject pra inferir estado (R1-R5 está morta).
    """
    base = Path(tooling_root) / "inbox"
    if not base.is_dir():
        return (0, 0)
    inserted = 0
    orphans = 0
    for path in base.rglob("*.md"):
        if not path.is_file():
            continue
        msg = parse_inbox_file(path)
        if msg is None:
            continue
        kind = "msg_send" if msg.from_ == "master" else "msg_receive"
        source_key = f"inbox:{path}"
        # Linkagem best-effort via thread + project
        ciclo_id = None
        if msg.thread:
            project_short = msg.to if msg.from_ == "master" else msg.from_
            if project_short in ("core", "cockpit", "aquarium", "logger"):
                ciclo_id = _find_ciclo_by_epic_project(
                    conn, msg.thread, project_short
                )
        if ciclo_id is None:
            orphans += 1
        payload = {
            "from": msg.from_,
            "to": msg.to,
            "type": msg.type,
            "subject": msg.subject,
            "thread": msg.thread,
            "path": str(path),
        }
        if _insert_evento_ignore(
            conn,
            source_key=source_key,
            ciclo_id=ciclo_id,
            ts=msg.created,
            kind=kind,
            severity="info",
            payload=payload,
        ):
            inserted += 1
    return (inserted, orphans)


# ── Linkage helpers ────────────────────────────────────────────────


def _find_ciclo_by_epic_project(
    conn: sqlite3.Connection,
    epic_slug: str,
    project_short: str,
) -> str | None:
    """Ciclo mais recente do par (épico, projeto). Heurística determinística."""
    project_full = f"mmb-{project_short}"
    row = conn.execute(
        """
        SELECT id FROM ciclos
        WHERE epico_id = ? AND project = ?
        ORDER BY planner_invoked_at DESC
        LIMIT 1
        """,
        (epic_slug, project_full),
    ).fetchone()
    return row["id"] if row else None


def _find_ciclo_for_journal(
    conn: sqlite3.Connection,
    epic: str | None,
    task: str | None,
) -> str | None:
    """Casa journal entry a ciclo via (epic, task) quando possível.

    task pode ser `core-X1` (agent-id com projeto embutido) ou `X1` (puro).
    Se contém projeto, casa exato. Senão, fallback pra latest ciclo do épico.
    Devolve None se nem epic existe.
    """
    if task:
        project_short, _ = decode_agent_id(task)
        if project_short and epic:
            return _find_ciclo_by_epic_project(conn, epic, project_short)
    if epic:
        row = conn.execute(
            """
            SELECT id FROM ciclos
            WHERE epico_id = ?
            ORDER BY planner_invoked_at DESC
            LIMIT 1
            """,
            (epic,),
        ).fetchone()
        return row["id"] if row else None
    return None


# ── DB primitive ───────────────────────────────────────────────────


def _insert_evento_ignore(
    conn: sqlite3.Connection,
    *,
    source_key: str,
    ciclo_id: str | None,
    ts: str,
    kind: str,
    severity: str | None,
    payload: dict,
) -> bool:
    """INSERT OR IGNORE em eventos. Retorna True se inseriu (False = dedup)."""
    payload_json = json.dumps(payload, ensure_ascii=False, default=str)
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO eventos
          (ciclo_id, ts, kind, severity, payload_json, source_key)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (ciclo_id, ts, kind, severity, payload_json, source_key),
    )
    return cur.rowcount > 0


def _iter_lines(path: Path) -> Iterator[str]:
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        yield from f
