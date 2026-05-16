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
    line = line.strip()
    if not line:
        return None
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict):
        return None
    if "ts" not in d or "sev" not in d or "ev" not in d:
        return None
    return JournalEntry(
        ts=str(d["ts"]),
        sev=str(d["sev"]),
        ev=str(d["ev"]),
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
