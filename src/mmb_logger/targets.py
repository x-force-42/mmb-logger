"""Cliente Python (read-only) do registry de targets do MMB.

Lê `.tooling/targets.json` — manifest declarativo que substitui as listas
hardcoded dos consumidores Python (reconcile, ingest). Espelho parcial de
`.tooling/lib/targets.sh`.

Stdlib-only (`json`, `pathlib`, `os`, `dataclasses`). Sem dep nova.

Resolução do path:
1. Argumento explícito `registry_path` (usado em testes).
2. Env `MMB_TARGETS_FILE` (override operacional).
3. Walk-up a partir do módulo até encontrar `<dir>/.tooling/targets.json`.
4. Falha com `FileNotFoundError` clara.

Cache module-level: chamadas sem `registry_path` reusam o último load.
Reload forçado: chamar `load_targets(path)` com path explícito invalida
o cache anterior.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

_REQUIRED_FIELDS = (
    "id",
    "dest",
    "repo",
    "local_path",
    "worker_profile",
    "agent_layer",
    "tracked_by_logger",
)


@dataclass(frozen=True)
class Target:
    id: str
    dest: str
    repo: str
    local_path: str
    worker_profile: str
    agent_layer: str
    tracked_by_logger: bool


_cache: list[Target] | None = None
_cache_path: Path | None = None


class RegistryError(ValueError):
    """Registry inválido (schema ou conteúdo)."""


def _resolve_path(explicit: Path | str | None) -> Path:
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("MMB_TARGETS_FILE")
    if env:
        return Path(env)
    # Walk-up a partir deste arquivo até achar .tooling/targets.json.
    # Layout esperado: <MMB_ROOT>/mmb-logger/src/mmb_logger/targets.py
    # → MMB_ROOT = parents[3]; mas walk-up é mais resiliente a layouts
    # alternativos (worktrees, smoke sandbox).
    here = Path(__file__).resolve().parent
    for ancestor in (here, *here.parents):
        candidate = ancestor / ".tooling" / "targets.json"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "Registry de targets não encontrado. Tentei walk-up a partir de "
        f"{here} procurando '.tooling/targets.json'. "
        "Defina MMB_TARGETS_FILE ou passe registry_path explícito."
    )


def _parse(data: object, path: Path) -> list[Target]:
    if not isinstance(data, dict):
        raise RegistryError(f"{path}: raiz não é objeto JSON")
    if data.get("schema_version") != 1:
        raise RegistryError(
            f"{path}: schema_version esperado 1, achei {data.get('schema_version')!r}"
        )
    raw_targets = data.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise RegistryError(f"{path}: 'targets' ausente ou array vazio")

    result: list[Target] = []
    for i, t in enumerate(raw_targets):
        if not isinstance(t, dict):
            raise RegistryError(f"{path}: target[{i}] não é objeto")
        extra = set(t) - set(_REQUIRED_FIELDS)
        if extra:
            raise RegistryError(
                f"{path}: target[{i}] tem campos extras: {sorted(extra)}"
            )
        missing = [k for k in _REQUIRED_FIELDS if k not in t]
        if missing:
            raise RegistryError(f"{path}: target[{i}] sem campos: {missing}")
        for k in _REQUIRED_FIELDS[:-1]:  # strings
            v = t[k]
            if not isinstance(v, str) or not v:
                raise RegistryError(
                    f"{path}: target[{i}].{k} não é string não-vazia ({v!r})"
                )
        if not isinstance(t["tracked_by_logger"], bool):
            raise RegistryError(
                f"{path}: target[{i}].tracked_by_logger não é booleano "
                f"({t['tracked_by_logger']!r})"
            )
        result.append(
            Target(
                id=t["id"],
                dest=t["dest"],
                repo=t["repo"],
                local_path=t["local_path"],
                worker_profile=t["worker_profile"],
                agent_layer=t["agent_layer"],
                tracked_by_logger=t["tracked_by_logger"],
            )
        )
    return result


def load_targets(registry_path: Path | str | None = None) -> list[Target]:
    """Carrega `.tooling/targets.json` e retorna lista de Target.

    Cache: chamadas sem `registry_path` reusam o último resultado.
    Path explícito sempre re-le.

    Raises:
        FileNotFoundError: registry não encontrado.
        RegistryError: schema inválido.
    """
    global _cache, _cache_path
    if registry_path is None and _cache is not None:
        return _cache
    path = _resolve_path(registry_path)
    if not path.is_file():
        raise FileNotFoundError(f"Registry não existe: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    targets = _parse(data, path)
    _cache = targets
    _cache_path = path
    return targets


def repos_tracked() -> tuple[str, ...]:
    """Repos full (mmb-<id>) de targets com `tracked_by_logger=True`.

    Para `fetch_issues`/`fetch_prs` no reconciler — só varre repos que
    o método declara como rastreáveis.
    """
    return tuple(t.repo for t in load_targets() if t.tracked_by_logger)


def target_ids() -> tuple[str, ...]:
    """Ids (= dests curtos) de todos os targets registrados.

    Lista canônica do que existe hoje em produção. Para validação
    prospectiva (mensagens novas, eventos novos).
    """
    return tuple(t.id for t in load_targets())


def historical_dest_ids() -> tuple[str, ...]:
    """Ids ativos + aliases históricos.

    **`core` é alias histórico, NÃO target ativo.** Foi target real
    do MMB até 2026-05 (commit b10b323 removeu mmb-core do andaime).
    Mensagens antigas em `.tooling/inbox/`, eventos em `agents.jsonl`,
    journals arquivados — todos referenciam `core`. O reconciler/ingest
    precisa tolerar isso para projetar histórico fielmente.

    **Não use em código de runtime/dispatch.** Para validar destinos
    de mensagens NOVAS, use `target_ids()`. `historical_dest_ids()`
    é exclusivo para parsing/validação de dados retrospectivos.

    Dívida explícita: quando todo histórico pré-2026-05 estiver fora
    do reconcile window (ou archived), `core` pode sair daqui.
    """
    return (*target_ids(), "core")
