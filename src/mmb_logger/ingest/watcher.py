"""Wrapper fino sobre `watchdog.observers.polling.PollingObserver`.

Polling é mais confiável que inotify em WSL2; a carga é baixa porque
os arquivos do andaime mudam pouco.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers.polling import PollingObserver

InboxCb = Callable[[str], None]
JsonlCb = Callable[[str], None]


class _InboxHandler(FileSystemEventHandler):
    def __init__(self, on_event: InboxCb) -> None:
        self.on_event = on_event

    def on_created(self, event: FileSystemEvent) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        path = str(event.src_path)
        if not path.endswith(".md"):
            return
        if any(seg in path for seg in ("/.processing/", "/.done/", "/.dead/")):
            return
        self.on_event(path)

    def on_modified(self, event: FileSystemEvent) -> None:  # type: ignore[override]
        # commd move arquivos entre subdirs — `modified` no diretório raiz
        # pode acontecer também. Mesmo critério do on_created.
        if event.is_directory:
            return
        path = str(event.src_path)
        if not path.endswith(".md"):
            return
        if any(seg in path for seg in ("/.processing/", "/.done/", "/.dead/")):
            return
        self.on_event(path)


class _JsonlHandler(FileSystemEventHandler):
    def __init__(self, target_path: Path, on_event: JsonlCb) -> None:
        self.target_path = target_path
        self.on_event = on_event

    def on_modified(self, event: FileSystemEvent) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        if Path(str(event.src_path)) == self.target_path:
            self.on_event(str(event.src_path))


def build_observer(
    *,
    tooling_root: Path,
    on_inbox: InboxCb,
    on_journal: JsonlCb,
    on_agents: JsonlCb,
    poll_timeout: float = 1.0,
) -> PollingObserver:
    """Monta um PollingObserver vigiando inbox/, journal.jsonl e agents.jsonl."""
    observer = PollingObserver(timeout=poll_timeout)

    inbox_path = tooling_root / "inbox"
    if inbox_path.is_dir():
        observer.schedule(_InboxHandler(on_inbox), str(inbox_path), recursive=True)

    journal_path = tooling_root / "logs" / "journal.jsonl"
    if journal_path.parent.is_dir():
        observer.schedule(
            _JsonlHandler(journal_path, on_journal), str(journal_path.parent), recursive=False
        )

    agents_path = tooling_root / "state" / "agents.jsonl"
    if agents_path.parent.is_dir():
        observer.schedule(
            _JsonlHandler(agents_path, on_agents), str(agents_path.parent), recursive=False
        )

    return observer
