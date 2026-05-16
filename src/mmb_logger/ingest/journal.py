"""Parser de `<tooling>/logs/journal.jsonl`.

Schema esperado por linha:
{"ts": "ISO8601", "sev": "warn|error|critical", "ev": "slug",
 "msg": "...", "epic": "slug?", "task": "id?", "resolves": "id?"}
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass
class JournalEntry:
    ts: str
    sev: str
    ev: str
    msg: str
    epic: str | None
    task: str | None
    resolves: str | None
    raw: dict


def parse_line(line: str) -> JournalEntry | None:
    """Parseia linha JSONL do `journal.jsonl`.

    Aceita dois formatos conviventes:

    - **log.sh** (audit warn/error/critical): `{ts, sev, ev, msg, epic, task, ...}`.
    - **commd internal** (ops events sem severity): `{ts, event, dest, file, pid, ...}`.

    Para commd entries, `ev` é populado de `event` e `sev` defaulta para
    None (info-level). Audit downstream filtra por sev presente.
    """
    line = line.strip()
    if not line:
        return None
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict):
        return None
    if "ts" not in d:
        return None
    # Aceita 'ev' (log.sh) ou 'event' (commd)
    ev_val = d.get("ev") or d.get("event")
    if not ev_val:
        return None
    sev_val = d.get("sev")  # None pra commd entries (info-level)
    return JournalEntry(
        ts=str(d["ts"]),
        sev=str(sev_val) if sev_val else "info",
        ev=str(ev_val),
        msg=str(d.get("msg", "")),
        epic=d.get("epic") or None,
        task=d.get("task") or None,
        resolves=d.get("resolves") or None,
        raw=d,
    )


def read_journal_after(path: str | Path, offset: int) -> Iterator[tuple[JournalEntry, int]]:
    """Itera entries do journal a partir de `offset` em bytes.

    Yields `(entry, new_offset)` — new_offset é a posição *após* a linha lida.
    """
    p = Path(path)
    if not p.is_file():
        return
    with p.open("rb") as f:
        f.seek(offset)
        while True:
            line = f.readline()
            if not line:
                break
            new_offset = f.tell()
            try:
                decoded = line.decode("utf-8")
            except UnicodeDecodeError:
                continue
            entry = parse_line(decoded)
            if entry is not None:
                yield entry, new_offset
