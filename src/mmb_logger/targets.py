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

# Campos opcionais (PR 2A — preparação para target externo). Defaults
# preservam comportamento dos targets internos atuais.
_OPTIONAL_FIELDS = ("owner", "requires_github", "kind", "managed_by_reset")
_KIND_VALUES = ("internal", "external", "external-fake")
_OPTIONAL_DEFAULTS: dict[str, object] = {
    "owner": None,  # None → caller usa MMB_GH_OWNER env
    "requires_github": True,
    "kind": "internal",
    "managed_by_reset": True,
}


@dataclass(frozen=True)
class Target:
    id: str
    dest: str
    repo: str
    local_path: str
    worker_profile: str
    agent_layer: str
    tracked_by_logger: bool
    # Opcionais (PR 2A):
    owner: str | None = None
    requires_github: bool = True
    kind: str = "internal"
    managed_by_reset: bool = True


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
    allowed = set(_REQUIRED_FIELDS) | set(_OPTIONAL_FIELDS)
    for i, t in enumerate(raw_targets):
        if not isinstance(t, dict):
            raise RegistryError(f"{path}: target[{i}] não é objeto")
        extra = set(t) - allowed
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
        # Validação dos opcionais quando presentes
        if "owner" in t and t["owner"] is not None and not isinstance(t["owner"], str):
            raise RegistryError(
                f"{path}: target[{i}].owner não é string ou null ({t['owner']!r})"
            )
        if "requires_github" in t and not isinstance(t["requires_github"], bool):
            raise RegistryError(
                f"{path}: target[{i}].requires_github não é booleano "
                f"({t['requires_github']!r})"
            )
        if "kind" in t and (
            not isinstance(t["kind"], str) or t["kind"] not in _KIND_VALUES
        ):
            raise RegistryError(
                f"{path}: target[{i}].kind inválido ({t.get('kind')!r}; "
                f"use {_KIND_VALUES})"
            )
        if "managed_by_reset" in t and not isinstance(t["managed_by_reset"], bool):
            raise RegistryError(
                f"{path}: target[{i}].managed_by_reset não é booleano "
                f"({t['managed_by_reset']!r})"
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
                owner=t.get("owner", _OPTIONAL_DEFAULTS["owner"]),
                requires_github=t.get(
                    "requires_github", _OPTIONAL_DEFAULTS["requires_github"]
                ),
                kind=t.get("kind", _OPTIONAL_DEFAULTS["kind"]),
                managed_by_reset=t.get(
                    "managed_by_reset", _OPTIONAL_DEFAULTS["managed_by_reset"]
                ),
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


def short_to_repo(project_short: str) -> str:
    """Resolve short id (`dest`) → repo name via registry.

    Para targets registrados, devolve `target.repo` — funciona pra
    internos (`mmb-cockpit`) e externos (`campo-premiado`) igualmente.

    Fallback retrocompat: shorts não encontrados (ou erro de load)
    devolvem `mmb-{short}` — preserva ciclos antigos rotulados com o
    prefixo embutido em `ciclos.project` antes de PR #34.
    """
    try:
        for t in load_targets():
            if t.id == project_short:
                return t.repo
    except Exception:
        pass
    return f"mmb-{project_short}"


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
