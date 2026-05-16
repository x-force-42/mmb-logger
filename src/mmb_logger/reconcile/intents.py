"""Leitor de `master-briefing.md` em `.tooling/intents/<date>-<slug>/`.

Extrai a intenção do épico (texto humano), usado pra preencher
`epicos.intencao` em vez do placeholder slug.

Convenção do diretório: `<YYYY-MM-DD>-<slug>` ou `<YYYY-MM-DDTHH-MM-SSZ>-<slug>`.
Match termina com `-<slug>`. Quando há múltiplos candidatos (mesmo slug em
datas diferentes — re-dispatches), escolhe o mais recente lexicograficamente
(prefixo de data ordena natural).
"""

from __future__ import annotations

import re
from pathlib import Path

# Tamanho máximo de intenção. Cockpit mostra a primeira linha; passar muito
# disso seria poluição. Truncamento conservador.
_MAX_INTENT_CHARS = 500

# Linha canônica de fechamento: 'Status: ✅' com variações de formatação markdown.
_CLOSED_RE = re.compile(r"^\s*[-*]?\s*Status:\s*.*✅", re.MULTILINE)


def _find_briefing_path(tooling_root: Path, epic_slug: str) -> Path | None:
    """Retorna o path do master-briefing.md mais recente pra `epic_slug`, ou None."""
    intents_dir = Path(tooling_root) / "intents"
    if not intents_dir.is_dir():
        return None

    candidates = [
        d for d in intents_dir.iterdir()
        if d.is_dir() and d.name.endswith(f"-{epic_slug}")
    ]
    if not candidates:
        return None

    chosen = sorted(candidates)[-1]
    briefing_path = chosen / "master-briefing.md"
    return briefing_path if briefing_path.is_file() else None


def load_briefing_text(tooling_root: Path, epic_slug: str) -> str | None:
    """Retorna o conteúdo bruto do master-briefing.md mais recente pra `epic_slug`.

    Retorna None se não encontrar dir, não encontrar master-briefing.md, ou
    arquivo vazio.
    """
    path = _find_briefing_path(tooling_root, epic_slug)
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return text or None


def parse_closed_marker(text: str) -> bool:
    """True se o briefing tem linha 'Status: ✅' (primeira que case).

    Tolera variações de formatação markdown ('- Status: ✅ fechado',
    '* Status: ✅', 'Status: ✅ fechado em 2026-...'). Outros emoji
    de status (🎯 ativo, ⏳ em execução, ❌ abortado) não fecham.

    Sem linha 'Status:' → False (briefing em execução ou template).
    """
    return bool(_CLOSED_RE.search(text))


def load_intent_text(tooling_root: Path, epic_slug: str) -> str | None:
    """Procura master-briefing.md pra `<slug>` e retorna a intenção.

    Estratégia de extração (primeiro hit ganha):
    1. Primeira linha que começa com `# ` (h1 markdown) — usa como intenção.
    2. Senão, primeira linha não-vazia, ignorando frontmatter (entre `---`).

    Retorna None se não encontrar dir, não encontrar master-briefing.md, ou
    arquivo vazio.
    """
    briefing_path = _find_briefing_path(tooling_root, epic_slug)
    if briefing_path is None:
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
