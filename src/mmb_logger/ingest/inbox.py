"""Parser de mensagens da inbox em `<tooling>/inbox/<dest>/*.md`."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mmb_logger.ingest.frontmatter import parse as parse_fm
from mmb_logger.targets import historical_dest_ids

# Slugs aceitos como projeto-alvo. Inclui targets atuais + 'core' como
# alias histórico (mensagens pré-2026-05). Ver historical_dest_ids.
_PROJECT_SLUGS: tuple[str, ...] = historical_dest_ids()


@dataclass
class InboxMessage:
    path: str
    from_: str
    to: str
    type: str
    subject: str
    thread: str | None
    created: str
    body: str
    summary: str | None = None

    @property
    def project_slug(self) -> str | None:
        """Slug do projeto-alvo (cockpit/aquarium/logger ou histórico) ou None se for master."""
        for repo in _PROJECT_SLUGS:
            if self.from_ == repo or self.to == repo:
                return repo
        return None


def parse_inbox_file(path: str | Path) -> InboxMessage | None:
    """Lê arquivo .md, parseia frontmatter, devolve InboxMessage.

    Retorna None se frontmatter não tem campos mínimos (from, to, type,
    created, subject).
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    parsed = parse_fm(text)
    fm = parsed.frontmatter
    required = ("from", "to", "type", "created", "subject")
    if not all(k in fm for k in required):
        return None
    return InboxMessage(
        path=str(p),
        from_=fm["from"],
        to=fm["to"],
        type=fm["type"],
        subject=fm["subject"],
        thread=fm.get("thread") or None,
        created=fm["created"],
        body=parsed.body,
        summary=(fm.get("summary") or None),
    )


# Subdirs que o commd usa pra histórico processado.
LIFECYCLE_SUBDIRS = (".processing", ".done", ".dead")


def is_lifecycle_path(path: str | Path) -> bool:
    """True se path está em um dos subdirs lifecycle (.processing/.done/.dead)."""
    parts = Path(path).parts
    return any(p in LIFECYCLE_SUBDIRS for p in parts)


def iter_inbox_files(tooling_root: str | Path) -> list[Path]:
    """Devolve todos os .md sob inbox/, incluindo subdirs lifecycle.

    Usado pelo `ingest-once` (catch-up de histórico). Watch mode ignora
    lifecycle subdirs.
    """
    base = Path(tooling_root) / "inbox"
    if not base.is_dir():
        return []
    return sorted(p for p in base.rglob("*.md") if p.is_file())


def iter_live_inbox_files(tooling_root: str | Path) -> list[Path]:
    """Apenas .md no nível raiz de inbox/<dest>/ — exclui lifecycle subdirs."""
    base = Path(tooling_root) / "inbox"
    if not base.is_dir():
        return []
    out: list[Path] = []
    for dest_dir in base.iterdir():
        if not dest_dir.is_dir():
            continue
        for f in dest_dir.glob("*.md"):
            if f.is_file():
                out.append(f)
    return sorted(out)
