"""Parser de `<tooling>/state/agents.jsonl`.

Schema esperado por linha:
{"ts": "ISO8601", "ev": "spawn|deregister|heartbeat",
 "id": "...", "parent": "...", "pane": "...", "pid": N,
 "reason": "...?", "task": "...?", "epic": "...?",
 "model": "claude-...?"}

`model` é gravado pelo andaime em eventos `spawn` (T1 do épico
logger-model-tracking). Linhas antigas/sem modelo conhecido vêm
ausentes — tolerância via `None`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass
class AgentEvent:
    ts: str
    ev: str
    id: str
    parent: str | None
    pane: str | None
    pid: int | None
    reason: str | None
    task: str | None
    epic: str | None
    model: str | None
    raw: dict


def parse_line(line: str) -> AgentEvent | None:
    line = line.strip()
    if not line:
        return None
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict):
        return None
    if "ts" not in d or "ev" not in d or "id" not in d:
        return None
    pid_val = d.get("pid")
    try:
        pid_int = int(pid_val) if pid_val is not None else None
    except (TypeError, ValueError):
        pid_int = None
    model_val = d.get("model")
    model_str = str(model_val) if isinstance(model_val, str) and model_val else None
    return AgentEvent(
        ts=str(d["ts"]),
        ev=str(d["ev"]),
        id=str(d["id"]),
        parent=d.get("parent") or None,
        pane=d.get("pane") or None,
        pid=pid_int,
        reason=d.get("reason") or None,
        task=d.get("task") or None,
        epic=d.get("epic") or None,
        model=model_str,
        raw=d,
    )


def read_agents_after(path: str | Path, offset: int) -> Iterator[tuple[AgentEvent, int]]:
    """Itera AgentEvents a partir de `offset` em bytes. Yields `(evt, new_offset)`."""
    p = Path(path)
    if not p.is_file():
        return
    with p.open("rb") as f:
        f.seek(offset)
        while True:
            line = f.readline()
            if not line:
                break
            new_offset = f.tell()
            try:
                decoded = line.decode("utf-8")
            except UnicodeDecodeError:
                continue
            evt = parse_line(decoded)
            if evt is not None:
                yield evt, new_offset


# Mapeia prefixo de id (`cockpit-M2` → `cockpit`) e task (`M2`).
def decode_agent_id(agent_id: str) -> tuple[str | None, str | None]:
    """Decodifica id no padrão `<project>-<task>` → (project_short, task_id).

    Retorna (None, None) se não casar com o padrão. Aceita ids como
    `master`, `commd`, etc. (project None).
    """
    if "-" not in agent_id:
        return (None, None)
    prefix, _, suffix = agent_id.partition("-")
    if prefix not in ("core", "cockpit", "aquarium", "logger"):
        return (None, None)
    return (prefix, suffix or None)
