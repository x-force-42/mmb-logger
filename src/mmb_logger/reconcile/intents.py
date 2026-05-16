"""Leitor de `master-briefing.md` em `.tooling/intents/<date>-<slug>/`.

Extrai a intenção do épico (texto humano), usado pra preencher
`epicos.intencao` em vez do placeholder slug.

Convenção do diretório: `<YYYY-MM-DD>-<slug>` ou `<YYYY-MM-DDTHH-MM-SSZ>-<slug>`.
Match termina com `-<slug>`. Quando há múltiplos candidatos (mesmo slug em
datas diferentes — re-dispatches), escolhe o mais recente lexicograficamente
(prefixo de data ordena natural).
"""

from __future__ import annotations

from pathlib import Path

# Tamanho máximo de intenção. Cockpit mostra a primeira linha; passar muito
# disso seria poluição. Truncamento conservador.
_MAX_INTENT_CHARS = 500


def load_intent_text(tooling_root: Path, epic_slug: str) -> str | None:
    """Procura master-briefing.md pra `<slug>` e retorna a intenção.

    Estratégia de extração (primeiro hit ganha):
    1. Primeira linha que começa com `# ` (h1 markdown) — usa como intenção.
    2. Senão, primeira linha não-vazia, ignorando frontmatter (entre `---`).

    Retorna None se não encontrar dir, não encontrar master-briefing.md, ou
    arquivo vazio.
    """
    intents_dir = Path(tooling_root) / "intents"
    if not intents_dir.is_dir():
        return None

    candidates = [
        d for d in intents_dir.iterdir()
        if d.is_dir() and d.name.endswith(f"-{epic_slug}")
    ]
    if not candidates:
        return None

    # Mais recente — prefixo de data ordena bem
    chosen = sorted(candidates)[-1]
    briefing_path = chosen / "master-briefing.md"
    if not briefing_path.is_file():
        return None

    try:
        text = briefing_path.read_text(encoding="utf-8")
    except OSError:
        return None

    in_frontmatter = False
    saw_open_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        # Frontmatter handling — pula bloco entre os primeiros dois "---"
        if stripped == "---":
            if not saw_open_fence:
                saw_open_fence = True
                in_frontmatter = True
                continue
            if in_frontmatter:
                in_frontmatter = False
                continue
        if in_frontmatter:
            continue
        if not stripped:
            continue
        # H1 markdown
        if stripped.startswith("# "):
            intent = stripped[2:].strip()
            return _truncate(intent)
        # Primeira linha não-vazia, não-h1
        return _truncate(stripped)

    return None


def _truncate(s: str) -> str:
    if len(s) <= _MAX_INTENT_CHARS:
        return s
    return s[: _MAX_INTENT_CHARS - 1] + "…"
