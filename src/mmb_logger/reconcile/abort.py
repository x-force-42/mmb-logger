"""Detecção de aborto pré-GH: sinais colaterais de falha.

Quatro sinais aceitos (per `.tooling/source-of-truth.md`):
  1. `commd-worker-exit` no `logs/journal.jsonl` casando project+janela.
  2. `commd-worker-timeout` no `logs/journal.jsonl`, idem.
  3. `agents.jsonl` `deregister` com `reason` classificável como erro
     (heartbeat/manual/self), casando project+janela.
  4. Stale: briefing órfão sem nenhum sinal acima por > threshold.

`abort_origin` em ciclos.abort_origin assume os valores estendidos:
  `worker-exit | worker-timeout | heartbeat | manual | self | stale`
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mmb_logger.ingest.agents_stream import decode_agent_id
from mmb_logger.ingest.agents_stream import parse_line as parse_agent_line
from mmb_logger.ingest.journal import parse_line as parse_journal_line

# Threshold default (segundos) para classificar briefing órfão como stale.
DEFAULT_STALE_THRESHOLD_S = 3600  # 1h

# Variável de env: MMB_LOGGER_STALE_THRESHOLD_S=<segundos>
ENV_STALE_THRESHOLD = "MMB_LOGGER_STALE_THRESHOLD_S"


@dataclass(frozen=True)
class AbortSignal:
    """Sinal de falha colateral apto a marcar um briefing como abortado pré-GH."""

    abort_at: str
    abort_origin: str
    abort_reason: str


@dataclass(frozen=True)
class _JournalWorkerSignal:
    ts: str
    ev: str  # commd-worker-exit | commd-worker-timeout
    dest: str | None  # short do projeto (core/cockpit/aquarium) ou None


@dataclass(frozen=True)
class _AgentDeregisterSignal:
    ts: str
    id: str
    project_short: str
    reason: str
    abort_origin: str  # heartbeat | manual | self


def resolve_stale_threshold_s(explicit: int | None = None) -> int:
    """Resolve threshold em ordem: explicit > env > default."""
    if explicit is not None:
        return int(explicit)
    env = os.environ.get(ENV_STALE_THRESHOLD)
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    return DEFAULT_STALE_THRESHOLD_S


def _iso_to_epoch(ts: str) -> float | None:
    """ISO8601 (sufixo Z ou offset explícito) → epoch seconds; None se inválido."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_journal_worker_signals(
    tooling_root: Path,
) -> list[_JournalWorkerSignal]:
    """Lê `logs/journal.jsonl` e devolve apenas eventos commd-worker-{exit,timeout}."""
    path = Path(tooling_root) / "logs" / "journal.jsonl"
    if not path.is_file():
        return []
    out: list[_JournalWorkerSignal] = []
    for line in _iter_lines(path):
        entry = parse_journal_line(line)
        if entry is None:
            continue
        if entry.ev not in ("commd-worker-exit", "commd-worker-timeout"):
            continue
        # commd grava `dest` no JSON cru; parseamos pelo `raw`.
        dest = entry.raw.get("dest") if isinstance(entry.raw, dict) else None
        if isinstance(dest, str):
            dest_str: str | None = dest
        else:
            dest_str = None
        out.append(_JournalWorkerSignal(ts=entry.ts, ev=entry.ev, dest=dest_str))
    return out


def load_agent_deregister_signals(
    tooling_root: Path,
) -> list[_AgentDeregisterSignal]:
    """Lê `state/agents.jsonl` e devolve deregisters classificados como erro."""
    path = Path(tooling_root) / "state" / "agents.jsonl"
    if not path.is_file():
        return []
    out: list[_AgentDeregisterSignal] = []
    for line in _iter_lines(path):
        evt = parse_agent_line(line)
        if evt is None or evt.ev != "deregister":
            continue
        origin = _classify_deregister_reason(evt.reason)
        if origin is None:
            continue
        project_short, _ = decode_agent_id(evt.id)
        if project_short is None:
            continue
        out.append(
            _AgentDeregisterSignal(
                ts=evt.ts,
                id=evt.id,
                project_short=project_short,
                reason=evt.reason or "",
                abort_origin=origin,
            )
        )
    return out


def _classify_deregister_reason(reason: str | None) -> str | None:
    """Mapeia `agents.jsonl.reason` → abort_origin.

    Devolve None se a razão for benigna (pr-opened, completed, etc) — esses
    NÃO sinalizam aborto. Padrão preserva semântica do `_classify_abort_origin`
    histórico de `inference.py`.
    """
    if not reason:
        return None
    r = reason.lower()
    if "pr-opened" in r or "completed" in r:
        return None  # encerramento limpo
    if "heartbeat" in r or "timeout" in r:
        return "heartbeat"
    if r.startswith("self") or "self-" in r:
        return "self"
    if "demo-end" in r or "manual" in r:
        return "manual"
    return None


def _iter_lines(path: Path) -> Iterator[str]:
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        yield from f


def detect_abort_signal(
    *,
    briefing_created: str,
    project_short: str,
    journal_signals: list[_JournalWorkerSignal],
    agent_signals: list[_AgentDeregisterSignal],
    now_epoch: float,
    stale_threshold_s: int,
) -> AbortSignal | None:
    """Decide se um briefing órfão deveria ser marcado como `abortado` pré-GH.

    Prioridade de sinais:
      1. Journal worker-exit/timeout no projeto, dentro de stale_threshold após briefing.
      2. Agent deregister classificado como erro no projeto, dentro da mesma janela.
      3. Stale: idade do briefing > threshold sem nenhum sinal acima.

    Retorna None se nenhum sinal aplicável (briefing permanece `iniciado`).
    """
    briefing_epoch = _iso_to_epoch(briefing_created)
    if briefing_epoch is None:
        return None

    # 1. Journal worker-* signals
    for sig in journal_signals:
        if sig.dest != project_short:
            continue
        sig_epoch = _iso_to_epoch(sig.ts)
        if sig_epoch is None:
            continue
        if sig_epoch < briefing_epoch:
            continue
        if sig_epoch > briefing_epoch + stale_threshold_s:
            continue
        origin = "worker-exit" if sig.ev == "commd-worker-exit" else "worker-timeout"
        return AbortSignal(
            abort_at=sig.ts,
            abort_origin=origin,
            abort_reason=f"{sig.ev} no dispatch do briefing (dest={sig.dest})",
        )

    # 2. Agent deregister signals
    for sig in agent_signals:
        if sig.project_short != project_short:
            continue
        sig_epoch = _iso_to_epoch(sig.ts)
        if sig_epoch is None:
            continue
        if sig_epoch < briefing_epoch:
            continue
        if sig_epoch > briefing_epoch + stale_threshold_s:
            continue
        return AbortSignal(
            abort_at=sig.ts,
            abort_origin=sig.abort_origin,
            abort_reason=sig.reason or "deregister sem reason",
        )

    # 3. Stale fallback — inferência por ausência+tempo.
    # Mais fraca que os 3 sinais acima (worker-exit/timeout/deregister): aqui
    # NÃO há sinal positivo de falha, apenas ausência prolongada de progresso.
    # Cockpit/revisor humano deve poder reclassificar manualmente se necessário.
    age = now_epoch - briefing_epoch
    if age > stale_threshold_s:
        age_s = int(age)
        return AbortSignal(
            abort_at=_now_iso(),
            abort_origin="stale",
            abort_reason=(
                f"stale: sem issue casada e sem sinal colateral "
                f"(worker-exit / worker-timeout / agents-deregister) em "
                f"{stale_threshold_s}s. "
                f"briefing criado em {briefing_created}, idade {age_s}s. "
                f"classificação por ausência+tempo — confiança inferior aos "
                f"outros 3 sinais; revisor humano pode reclassificar."
            ),
        )

    return None
