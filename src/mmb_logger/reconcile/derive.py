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

# Closes/Fixes/Resolves [owner/repo]#N — convenção de PR body que GH
# reconhece pra fechar issue automaticamente. Aceita ambos:
#   - `Closes #N` (mono-repo, conveniência GH).
#   - `Closes owner/repo#N` (cross-repo, formato GH oficial).
# Case-insensitive. Captura owner/repo opcionais separados do número.
_CLOSES_RE = re.compile(
    r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+"
    r"(?:([\w.-]+)/([\w.-]+))?#(\d+)",
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


class CloseRef(NamedTuple):
    """Referência Close/Fix/Resolve em PR body.

    `owner` e `repo` são `None` quando o ref usa o formato mono-repo
    (`Closes #N`). Quando o ref usa o formato cross-repo
    (`Closes owner/repo#N`), ambos vêm populados — o consumidor decide
    se a referência é válida (mesmo repo do PR) ou cross-repo a ignorar.
    """

    owner: str | None
    repo: str | None
    issue: int


def parse_closes(body: str) -> list[CloseRef]:
    """Todas as refs Close/Fix/Resolve no body de um PR.

    Aceita ambos formatos GH (mono-repo `#N` e cross-repo `owner/repo#N`),
    case-insensitive. A política de mesmo-repo vs. cross-repo é
    responsabilidade do caller — ciclo do logger é por-repo, então PRs
    fechando issues em outros repos devem ser ignorados pela linkagem
    (`reconcile.link_pr_to_issue` emite warning + skip).
    """
    if not body:
        return []
    refs: list[CloseRef] = []
    for m in _CLOSES_RE.finditer(body):
        owner = m.group(1) or None
        repo = m.group(2) or None
        refs.append(CloseRef(owner, repo, int(m.group(3))))
    return refs


def epic_from_labels(labels: tuple[str, ...]) -> str | None:
    """Procura primeiro label `epic:<slug>` e devolve `<slug>`."""
    for label in labels:
        m = _EPIC_LABEL_RE.match(label)
        if m:
            return m.group(1).strip()
    return None
