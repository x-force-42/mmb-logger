"""Orquestração da ingestão.

`ingest_once(db_path, tooling_root)` varre os 3 fluxos uma vez,
idempotente. `watch()` é o loop persistente — roda `ingest_once` primeiro
e depois delega ao `watcher`.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from mmb_logger.db import (
    get_conn,
    get_jsonl_offset,
    is_file_processed,
    mark_file_processed,
    set_jsonl_offset,
    upsert_projeto,
)
from mmb_logger.ingest.agents_stream import read_agents_after
from mmb_logger.ingest.inbox import is_lifecycle_path, iter_inbox_files, parse_inbox_file
from mmb_logger.ingest.inference import (
    apply_agent_event,
    apply_inbox_message,
    apply_journal_entry,
)
from mmb_logger.ingest.journal import read_journal_after

DEFAULT_TOOLING = Path("/home/eliezer/llab/MMB/.tooling")


def resolve_andaime_version(tooling_root: Path) -> str | None:
    """Lê versão do andaime via `git describe --tags --abbrev=0` no repo MMB."""
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            cwd=str(tooling_root.parent),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except Exception:
        pass
    return None


def resolve_tooling_root(explicit: str | os.PathLike[str] | None = None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.environ.get("MMB_LOGGER_TOOLING_PATH")
    if env:
        return Path(env)
    return DEFAULT_TOOLING


def _seed_projetos(conn) -> None:
    """Garante que os 3 projetos canônicos existam em `projetos`."""
    base = "/home/eliezer/llab/MMB"
    for slug, name in (
        ("mmb-core", "MMB Core"),
        ("mmb-cockpit", "MMB Cockpit"),
        ("mmb-aquarium", "MMB Aquarium"),
    ):
        upsert_projeto(
            conn,
            id=slug,
            slug=slug,
            name=name,
            path=f"{base}/{slug}",
            repo_url=f"git@github.com:x-force-42/{slug}.git",
        )


def ingest_inbox_files(
    conn, tooling_root: Path, andaime_version: str | None = None
) -> tuple[int, int]:
    """Varre todos os .md do inbox (inclusive lifecycle). Retorna (novos, total)."""
    files = iter_inbox_files(tooling_root)
    novos = 0
    for f in files:
        path_str = str(f)
        if is_file_processed(conn, path_str):
            continue
        msg = parse_inbox_file(path_str)
        if msg is None:
            mark_file_processed(conn, path_str)
            continue
        apply_inbox_message(conn, msg, andaime_version=andaime_version)
        mark_file_processed(conn, path_str)
        novos += 1
    return novos, len(files)


def ingest_journal(conn, tooling_root: Path) -> int:
    """Lê journal.jsonl a partir do offset persistido. Retorna nº entries novas."""
    path = tooling_root / "logs" / "journal.jsonl"
    if not path.is_file():
        return 0
    offset = get_jsonl_offset(conn, "journal")
    novas = 0
    last_offset = offset
    for entry, new_offset in read_journal_after(path, offset):
        apply_journal_entry(conn, entry)
        last_offset = new_offset
        novas += 1
    if last_offset != offset:
        set_jsonl_offset(conn, "journal", last_offset)
    return novas


def ingest_agents(conn, tooling_root: Path) -> int:
    """Lê agents.jsonl a partir do offset persistido. Retorna nº eventos novos."""
    path = tooling_root / "state" / "agents.jsonl"
    if not path.is_file():
        return 0
    offset = get_jsonl_offset(conn, "agents")
    novas = 0
    last_offset = offset
    for evt, new_offset in read_agents_after(path, offset):
        apply_agent_event(conn, evt)
        last_offset = new_offset
        novas += 1
    if last_offset != offset:
        set_jsonl_offset(conn, "agents", last_offset)
    return novas


def ingest_once(
    db_path: str | os.PathLike[str] | None = None,
    tooling_root: str | os.PathLike[str] | None = None,
) -> dict[str, int]:
    """Varredura única, idempotente. Devolve contadores."""
    root = resolve_tooling_root(tooling_root)
    version = resolve_andaime_version(root)
    with get_conn(db_path) as conn:
        _seed_projetos(conn)
        novos_inbox, total_inbox = ingest_inbox_files(conn, root, andaime_version=version)
        novos_journal = ingest_journal(conn, root)
        novos_agents = ingest_agents(conn, root)
    return {
        "inbox_novos": novos_inbox,
        "inbox_total": total_inbox,
        "journal_novos": novos_journal,
        "agents_novos": novos_agents,
    }


def watch(
    db_path: str | os.PathLike[str] | None = None,
    tooling_root: str | os.PathLike[str] | None = None,
    poll_interval_s: float = 1.0,
) -> None:
    """Loop de ingestão contínua.

    - Roda `ingest_once` primeiro (catch-up).
    - Sobe watchdog Observer nos 3 caminhos relevantes.
    - Re-ingesta inbox/journal/agents periodicamente como fallback (polling).
    - SIGINT (Ctrl+C) desliga limpo.
    """
    from mmb_logger.ingest.watcher import build_observer

    root = resolve_tooling_root(tooling_root)
    print(f"[mmb-logger:watch] catch-up inicial (tooling_root={root})", file=sys.stderr)
    counts = ingest_once(db_path=db_path, tooling_root=root)
    print(f"[mmb-logger:watch] catch-up: {counts}", file=sys.stderr)

    stopping = {"flag": False}

    def _trigger_ingest(_event_path: str | None = None) -> None:
        try:
            ingest_once(db_path=db_path, tooling_root=root)
        except Exception as exc:  # pragma: no cover - watch is operational
            print(f"[mmb-logger:watch] ingest falhou: {exc}", file=sys.stderr)

    def _on_inbox_change(path: str) -> None:
        if is_lifecycle_path(path):
            return
        _trigger_ingest(path)

    observer = build_observer(
        tooling_root=root,
        on_inbox=_on_inbox_change,
        on_journal=_trigger_ingest,
        on_agents=_trigger_ingest,
    )
    observer.start()
    print("[mmb-logger:watch] observer ativo. Ctrl+C pra sair.", file=sys.stderr)

    def _handle_sigint(_signum, _frame) -> None:
        stopping["flag"] = True

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    try:
        while not stopping["flag"]:
            time.sleep(poll_interval_s)
            # Poll-fallback: garante progresso mesmo se watcher pular evento.
            _trigger_ingest()
    finally:
        observer.stop()
        observer.join(timeout=5)
        print("[mmb-logger:watch] desligado.", file=sys.stderr)
