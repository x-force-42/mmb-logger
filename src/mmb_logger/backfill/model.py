"""Backfill heurístico de `ciclos.model` para ciclos pré-T1.

T1+T2 introduziram captura de modelo via `state/agents.jsonl` spawn
events do planner. Ciclos anteriores ficam com `model IS NULL`. Este
módulo preenche o gap retroativamente, mapeando janelas temporais do
default de `MMB_MODE` no andaime (lido de `git log -- .tooling/config.sh`)
pro modelo do planner default daquele modo.

Características:
- **Heurístico**: assume que ninguém sobrescreveu `MMB_MODEL_*` via env
  fora do default do modo. Ciclos onde isso aconteceu ficam com modelo
  errado — sem como detectar.
- **Idempotente**: re-rodar não regride dados; só toca `model IS NULL`.
  Warnings ambíguos são deduplicados via leitura do journal existente.
- **Não-destrutivo a humanos**: filtros respeitam o contrato derivado
  vs humano (`assertiveness_score`/`review_note` não são afetados;
  UPDATE só toca `model`).
- **Pre-window**: ciclos com `planner_invoked_at` anteriores ao primeiro
  commit que introduziu `MMB_MODE` no andaime são pré-método. Abortados
  desse período são deixados como NULL sem warning (borda histórica
  documentada no briefing T3). Outros estados também NULL, mas com
  warning estruturado pro journal.

Coluna-âncora: `planner_invoked_at`. É o instante em que o planner foi
spawnado — momento em que o modelo foi efetivamente escolhido pelo
andaime. `started_at` (sugerido no briefing) é coluna de épico, não
de ciclo; `planner_invoked_at` é a equivalência semântica no domínio
de ciclos.
"""

from __future__ import annotations

import fcntl
import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from mmb_logger.db import get_conn
from mmb_logger.reconcile._runtime import resolve_tooling_root

# ── Constantes do mapeamento mode → modelo do planner ───────────────────
#
# Espelha `.tooling/config.sh` no estado atual (2026-05). Histórico
# inspecionado mostra que esses IDs nunca mudaram desde a primeira versão
# de `config.sh` — só a estrutura (introdução de MMB_MODE em 2026-05-14)
# variou. Se um dia o registry mudar, este dict precisa ser revisitado.

PLANNER_MODEL_BY_MODE: dict[str, str] = {
    "normal": "claude-opus-4-7",
    "fast": "claude-haiku-4-5-20251001",
    "balanced": "claude-sonnet-4-6",
}

WARNING_EVENT_AMBIGUOUS = "backfill-model-ambiguous"
BACKFILL_AGENT_ID = "mmb-logger-backfill"

_MMB_MODE_RE = re.compile(r':\s*"\$\{MMB_MODE:=(\w+)\}"')


# ── Janelas temporais ──────────────────────────────────────────────────


@dataclass(frozen=True)
class ModeWindow:
    """Janela `[start, end)` em UTC com modo MMB_MODE default associado."""

    start: datetime
    end: datetime | None  # exclusive; None = janela aberta (corrente)
    mode: str  # 'normal' | 'fast' | 'balanced' | 'pre-mmb-mode'

    @property
    def planner_model(self) -> str | None:
        return PLANNER_MODEL_BY_MODE.get(self.mode)


def _git(repo_root: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


def parse_mmb_mode_windows(
    repo_root: Path,
    config_relpath: str = ".tooling/config.sh",
) -> list[ModeWindow]:
    """Lê `git log --follow` de `config.sh` e gera janelas por mode default.

    Para cada commit que tocou o arquivo, extrai o valor default de
    `MMB_MODE` via regex `: "${MMB_MODE:=<X>}"`. Commits sem essa linha
    (pré-introdução do `MMB_MODE`, andaime v0) viram modo `pre-mmb-mode`.
    Janelas consecutivas com mesmo modo são coalescidas.

    Args:
      repo_root: raiz do repo git que hospeda `config.sh` (i.e., /MMB).
      config_relpath: caminho relativo de `config.sh` dentro do repo.

    Returns:
      Lista de `ModeWindow` ordenada por `start` ascendente. A última
      janela tem `end = None` (ainda corrente).
    """
    log_out = _git(
        repo_root,
        "log",
        "--follow",
        "--reverse",
        "--pretty=format:%H|%aI",
        "--",
        config_relpath,
    )
    transitions: list[tuple[datetime, str]] = []
    for line in log_out.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        sha, iso = line.split("|", 1)
        try:
            ts = datetime.fromisoformat(iso).astimezone(UTC)
        except ValueError:
            continue
        try:
            content = _git(repo_root, "show", f"{sha}:{config_relpath}")
        except subprocess.CalledProcessError:
            continue
        m = _MMB_MODE_RE.search(content)
        mode = m.group(1) if m else "pre-mmb-mode"
        if transitions and transitions[-1][1] == mode:
            continue  # coalesce consecutive duplicates
        transitions.append((ts, mode))

    windows: list[ModeWindow] = []
    for i, (ts, mode) in enumerate(transitions):
        end: datetime | None = transitions[i + 1][0] if i + 1 < len(transitions) else None
        windows.append(ModeWindow(start=ts, end=end, mode=mode))
    return windows


def find_window(windows: list[ModeWindow], ts: datetime) -> ModeWindow | None:
    """Retorna a janela que contém `ts`, ou None se nenhuma."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    for w in windows:
        if ts < w.start:
            continue
        if w.end is None or ts < w.end:
            return w
    return None


def _earliest_normal_window_start(windows: list[ModeWindow]) -> datetime | None:
    """Início da primeira janela com mode='normal' (referência do briefing T3)."""
    for w in windows:
        if w.mode == "normal":
            return w.start
    return None


# ── Journal: leitura (dedup) e escrita (warning estruturado) ────────────


def _existing_warned_cycle_ids(journal_path: Path) -> set[str]:
    """Set de cycle_ids que já têm `backfill-model-ambiguous` no journal."""
    if not journal_path.is_file():
        return set()
    warned: set[str] = set()
    with journal_path.open("r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                d = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(d, dict):
                continue
            ev = d.get("event") or d.get("ev")
            if ev != WARNING_EVENT_AMBIGUOUS:
                continue
            payload = d.get("payload")
            if isinstance(payload, dict):
                cid = payload.get("cycle_id")
                if isinstance(cid, str):
                    warned.add(cid)
    return warned


def _append_journal_entry(journal_path: Path, entry: dict) -> None:
    """Append JSON line ao journal sob `flock` (mesmo lockfile que log.sh)."""
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    if not journal_path.exists():
        journal_path.touch()
    lock_path = journal_path.parent / ".journal.lock"
    payload = json.dumps(entry, ensure_ascii=False, default=str)
    with lock_path.open("a") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            with journal_path.open("a", encoding="utf-8") as out:
                out.write(payload + "\n")
        finally:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)


def _build_ambiguous_entry(
    *,
    cycle_id: str,
    epic_id: str,
    planner_invoked_at: str,
    reason: str,
) -> dict:
    return {
        "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "agent": BACKFILL_AGENT_ID,
        "epic": epic_id,
        "task": cycle_id,
        "sev": "warn",
        "event": WARNING_EVENT_AMBIGUOUS,
        "msg": (
            f"cycle_id={cycle_id} planner_invoked_at={planner_invoked_at} "
            f"reason={reason}"
        ),
        "payload": {
            "cycle_id": cycle_id,
            "epic_id": epic_id,
            "planner_invoked_at": planner_invoked_at,
            "reason": reason,
        },
    }


# ── Execução do backfill ───────────────────────────────────────────────


@dataclass
class BackfillResult:
    candidates: int = 0                 # ciclos com model IS NULL + closed_complete_at NOT NULL
    backfilled: int = 0                 # model populado (ou seria, em dry-run)
    ambiguous: int = 0                  # ficaram NULL com warning
    skipped_pre_window_abort: int = 0   # abortados pré-MMB_MODE: silenciados
    by_model: dict[str, int] = field(default_factory=dict)
    warnings_emitted: int = 0
    warnings_skipped_dedup: int = 0
    dry_run: bool = False
    ambiguous_samples: list[dict] = field(default_factory=list)  # até 10 pra inspeção


def _parse_ts(value: str) -> datetime | None:
    """Parse ISO8601 (com Z ou offset) em datetime UTC. None se falhar."""
    if not value:
        return None
    try:
        s = value.replace("Z", "+00:00") if value.endswith("Z") else value
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        return None


def backfill_model(
    *,
    db_path: str | Path | None = None,
    tooling_root: str | Path | None = None,
    repo_root: str | Path | None = None,
    journal_path: str | Path | None = None,
    windows: list[ModeWindow] | None = None,
    dry_run: bool = False,
) -> BackfillResult:
    """Roda o backfill heurístico de `ciclos.model`.

    Args:
      db_path: SQLite path. Default: env `MMB_LOGGER_DB_PATH` ou `./mmb-logger.db`.
      tooling_root: raiz de `.tooling/`. Default: env `MMB_LOGGER_TOOLING_PATH`
        ou `/home/eliezer/llab/MMB/.tooling`.
      repo_root: raiz do repo git que hospeda `.tooling/config.sh`. Default:
        `tooling_root.parent`.
      journal_path: `journal.jsonl` pra dedup e emissão de warnings. Default:
        `tooling_root/logs/journal.jsonl`.
      windows: lista pré-computada de `ModeWindow`. None → parseia do git log.
        Útil pra testes injetarem janelas sem depender de um repo git real.
      dry_run: se True, não escreve no DB nem no journal. Retorna contagens.

    Returns:
      `BackfillResult` com contagens e samples ambíguos pra inspeção.
    """
    tooling_path = resolve_tooling_root(tooling_root)
    if repo_root is None:
        repo_root_path = tooling_path.parent
    else:
        repo_root_path = Path(repo_root)
    if journal_path is None:
        journal_file = tooling_path / "logs" / "journal.jsonl"
    else:
        journal_file = Path(journal_path)

    if windows is None:
        try:
            windows = parse_mmb_mode_windows(repo_root_path)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"backfill-model: falha lendo git log de config.sh em {repo_root_path}: "
                f"{exc.stderr or exc}"
            ) from exc

    normal_start = _earliest_normal_window_start(windows)

    result = BackfillResult(dry_run=dry_run)
    already_warned = _existing_warned_cycle_ids(journal_file)

    with get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, epico_id, planner_invoked_at, status
            FROM ciclos
            WHERE model IS NULL
              AND closed_complete_at IS NOT NULL
            """
        ).fetchall()

        result.candidates = len(rows)

        for row in rows:
            cycle_id = row["id"]
            epic_id = row["epico_id"]
            invoked_at = row["planner_invoked_at"]
            status = row["status"]

            ts = _parse_ts(invoked_at)
            if ts is None:
                _handle_ambiguous(
                    result,
                    journal_file=journal_file,
                    already_warned=already_warned,
                    cycle_id=cycle_id,
                    epic_id=epic_id,
                    invoked_at=invoked_at,
                    reason="unparseable-planner-invoked-at",
                    dry_run=dry_run,
                )
                continue

            # Pré-janela normal: borda histórica. Abortados são silenciados.
            if (
                normal_start is not None
                and ts < normal_start
                and status == "abortado"
            ):
                result.skipped_pre_window_abort += 1
                continue

            window = find_window(windows, ts)
            if window is None or window.planner_model is None:
                _handle_ambiguous(
                    result,
                    journal_file=journal_file,
                    already_warned=already_warned,
                    cycle_id=cycle_id,
                    epic_id=epic_id,
                    invoked_at=invoked_at,
                    reason=(
                        "no-window-match"
                        if window is None
                        else f"mode-without-model:{window.mode}"
                    ),
                    dry_run=dry_run,
                )
                continue

            model = window.planner_model
            if not dry_run:
                # Filtro WHERE model IS NULL é defensivo: garante que mesmo
                # se outro processo concorrente popular `model`, não
                # sobrescrevemos. Idempotência forte.
                conn.execute(
                    "UPDATE ciclos SET model = ? WHERE id = ? AND model IS NULL",
                    (model, cycle_id),
                )
            result.backfilled += 1
            result.by_model[model] = result.by_model.get(model, 0) + 1

    return result


def _handle_ambiguous(
    result: BackfillResult,
    *,
    journal_file: Path,
    already_warned: set[str],
    cycle_id: str,
    epic_id: str,
    invoked_at: str,
    reason: str,
    dry_run: bool,
) -> None:
    result.ambiguous += 1
    if len(result.ambiguous_samples) < 10:
        result.ambiguous_samples.append(
            {
                "cycle_id": cycle_id,
                "epic_id": epic_id,
                "planner_invoked_at": invoked_at,
                "reason": reason,
            }
        )
    if cycle_id in already_warned:
        result.warnings_skipped_dedup += 1
        return
    if dry_run:
        # Dry-run: contabiliza mas não escreve. Próxima run real emite.
        return
    entry = _build_ambiguous_entry(
        cycle_id=cycle_id,
        epic_id=epic_id,
        planner_invoked_at=invoked_at,
        reason=reason,
    )
    _append_journal_entry(journal_file, entry)
    already_warned.add(cycle_id)
    result.warnings_emitted += 1
