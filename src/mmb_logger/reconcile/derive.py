"""Funções puras de derivação.

Parse de âncora `mmb-cycle-key`, parse de `Closes #N`, leitura de label
`epic:<slug>`. Tudo testável sem rede, sem DB.
"""

from __future__ import annotations

import re
from typing import NamedTuple

# Bloco HTML-comment do contrato. Aceita whitespace livre, ordem
# fixa (cycle-key antes de briefing-file). briefing-file é opcional
# no parse — divergência detectada pelo reconcile como warning.
_ANCHOR_RE = re.compile(
    r"<!--\s*mmb-cycle-key:\s*(\S+)"
    r"(?:\s+mmb-briefing-file:\s*(\S+))?"
    r"\s*-->",
    re.IGNORECASE,
)

# Closes/Fixes/Resolves #N — convenção de PR body que GH reconhece
# pra fechar issue automaticamente. Captura todos os números.
_CLOSES_RE = re.compile(
    r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)",
    re.IGNORECASE,
)

_EPIC_LABEL_RE = re.compile(r"^epic:(.+)$")


class CycleKey(NamedTuple):
    epic_slug: str
    project_short: str
    briefing_ts: str
    briefing_file: str | None

    @property
    def cycle_key(self) -> str:
        """String canônica usada como chave de matching com `Briefing.cycle_key`."""
        return f"{self.epic_slug}/{self.project_short}/{self.briefing_ts}"


def parse_anchor(body: str) -> CycleKey | None:
    """Extrai `mmb-cycle-key` do body de uma issue. None se ausente."""
    if not body:
        return None
    m = _ANCHOR_RE.search(body)
    if not m:
        return None
    key = m.group(1).strip()
    parts = key.split("/")
    if len(parts) != 3:
        return None
    epic, project, ts = parts
    briefing_file = m.group(2).strip() if m.group(2) else None
    return CycleKey(epic, project, ts, briefing_file)


def parse_closes(body: str) -> list[int]:
    """Todos os números de issue referenciados via Closes/Fixes/Resolves."""
    if not body:
        return []
    return [int(m.group(1)) for m in _CLOSES_RE.finditer(body)]


def epic_from_labels(labels: tuple[str, ...]) -> str | None:
    """Procura primeiro label `epic:<slug>` e devolve `<slug>`."""
    for label in labels:
        m = _EPIC_LABEL_RE.match(label)
        if m:
            return m.group(1).strip()
    return None
