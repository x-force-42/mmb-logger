"""Mapeia spawns de planner em `state/agents.jsonl` → modelo do ciclo.

Política (do briefing logger-model-tracking T2.2):
- Só agentes planner contam: id ∈ {core, cockpit, aquarium, logger}.
  Atômicos (ex.: `core-X1`) têm modelo próprio mas não populam
  `ciclos.model`.
- Mapeamento: (epic_slug, project_short) → primeiro `model` presente
  em evento `spawn`. Múltiplos spawns do mesmo planner no mesmo
  ciclo (raro): primeiro vence.
- Tolerância: `model` ausente na linha → entrada ignorada (sem warn).
"""

from __future__ import annotations

from pathlib import Path

from mmb_logger.ingest.agents_stream import parse_line as parse_agent_line

_PLANNER_IDS = {"core", "cockpit", "aquarium", "logger"}


def load_planner_models(tooling_root: Path) -> dict[tuple[str, str], str]:
    """Lê `state/agents.jsonl` e retorna `{(epic_slug, project_short): model}`."""
    out: dict[tuple[str, str], str] = {}
    path = Path(tooling_root) / "state" / "agents.jsonl"
    if not path.is_file():
        return out
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            evt = parse_agent_line(line)
            if evt is None or evt.ev != "spawn":
                continue
            if evt.id not in _PLANNER_IDS:
                continue
            if not evt.epic or not evt.model:
                continue
            key = (evt.epic, evt.id)
            out.setdefault(key, evt.model)
    return out
