"""Leitor de briefings master→planner em `.tooling/inbox/<repo-short>/**`.

Reaproveita parsers de `ingest/inbox.py` e `ingest/frontmatter.py`.
Não chama `inference.py` — leitura é estritamente para alimentar o
reconciler com nascimentos de ciclo (fase 2).

Inclui arquivos em subdirs `.processing/`, `.done/`, `.dead/`. Briefings
malformados (sem frontmatter mínimo ou sem `thread`/`created`) viram
lista separada que o reconciler reporta como warning.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mmb_logger.ingest.inbox import parse_inbox_file

# Project shorts válidos como destinatários de briefing.
REPOS_SHORT = ("core", "cockpit", "aquarium", "logger")


@dataclass(frozen=True)
class Briefing:
    """Briefing master→planner extraído de um arquivo de inbox.

    Identificado unicamente pela tupla (epic_slug, project_short, created),
    que também forma a chave-canônica usada como `mmb-cycle-key` na
    âncora do issue body.
    """

    path: str
    epic_slug: str
    project_short: str
    created: str
    subject: str
    body: str

    @property
    def cycle_key(self) -> str:
        """Chave canônica usada na âncora `mmb-cycle-key` de issues."""
        return f"{self.epic_slug}/{self.project_short}/{self.created}"

    @property
    def cycle_id(self) -> str:
        """Natural key na coluna `ciclos.id`."""
        return f"{self.epic_slug}__{self.project_short}__{self.created}"


@dataclass(frozen=True)
class BriefingsLoaded:
    briefings: list[Briefing]
    malformed_paths: list[str]


def load_briefings(tooling_root: Path) -> BriefingsLoaded:
    """Coleta todos os briefings master→planner sob `inbox/`.

    Filename pattern: `*_master_briefing_*.md`. Inclui top-level e
    subdirs lifecycle (`.processing`, `.done`, `.dead`) via rglob.

    Critério "malformado" (vira warning, não vira ciclo):
      - parse_inbox_file falha (frontmatter ausente ou incompleto).
      - frontmatter.from != "master" ou .type != "briefing".
      - frontmatter.to ∉ REPOS_SHORT.
      - frontmatter.thread ou .created vazios.

    Ordena briefings válidos por `created` asc.
    """
    base = Path(tooling_root) / "inbox"
    if not base.is_dir():
        return BriefingsLoaded(briefings=[], malformed_paths=[])

    briefings: list[Briefing] = []
    malformed: list[str] = []

    for path in base.rglob("*_master_briefing_*.md"):
        if not path.is_file():
            continue
        msg = parse_inbox_file(path)
        if msg is None:
            malformed.append(str(path))
            continue
        if msg.from_ != "master" or msg.type != "briefing":
            malformed.append(str(path))
            continue
        if msg.to not in REPOS_SHORT:
            malformed.append(str(path))
            continue
        if not msg.thread or not msg.created:
            malformed.append(str(path))
            continue
        briefings.append(
            Briefing(
                path=str(path),
                epic_slug=msg.thread,
                project_short=msg.to,
                created=msg.created,
                subject=msg.subject,
                body=msg.body,
            )
        )

    briefings.sort(key=lambda b: b.created)
    return BriefingsLoaded(briefings=briefings, malformed_paths=sorted(malformed))
