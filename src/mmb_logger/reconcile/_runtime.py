"""Helpers de runtime do reconciler.

Resolução de paths e versão do andaime. Migrado de `ingest/runner.py`
(removido na fase 3) pra desacoplar o reconcile do código legado de
inferência por subject.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

DEFAULT_TOOLING = Path("/home/eliezer/llab/MMB/.tooling")


def resolve_tooling_root(
    explicit: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve raiz do `.tooling/`: argumento > env > default."""
    if explicit:
        return Path(explicit)
    env = os.environ.get("MMB_LOGGER_TOOLING_PATH")
    if env:
        return Path(env)
    return DEFAULT_TOOLING


def resolve_andaime_version(tooling_root: Path) -> str | None:
    """Versão do andaime via `git describe --tags --abbrev=0` no repo MMB."""
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
