"""Fetcher do GitHub via `gh` CLI.

Funções devolvem dataclasses imutáveis. Subprocess pra `gh`; falha vira
RuntimeError. Mockável nos testes via injeção de função.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass

REPOS = ("mmb-core", "mmb-cockpit", "mmb-aquarium", "mmb-logger")


@dataclass(frozen=True)
class GhIssue:
    repo: str
    number: int
    title: str
    body: str
    state: str  # "OPEN" | "CLOSED"
    labels: tuple[str, ...]
    created_at: str
    closed_at: str | None


@dataclass(frozen=True)
class GhPr:
    repo: str
    number: int
    title: str
    body: str
    state: str  # "OPEN" | "CLOSED" | "MERGED"
    url: str
    created_at: str
    merged_at: str | None
    head_ref_name: str
    additions: int
    deletions: int
    changed_files: int


def _run_gh(args: list[str]) -> list[dict]:
    if not shutil.which("gh"):
        raise RuntimeError("gh CLI não encontrado no PATH. Instale https://cli.github.com/")
    res = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise RuntimeError(f"gh falhou ({res.returncode}): {' '.join(args)}\n{res.stderr}")
    out = res.stdout.strip()
    if not out:
        return []
    parsed = json.loads(out)
    if not isinstance(parsed, list):
        raise RuntimeError(f"gh devolveu JSON não-lista: {out[:200]}")
    return parsed


def fetch_issues(owner: str, repo: str, limit: int = 1000) -> list[GhIssue]:
    """Lista issues (todos os estados) com campos necessários pro reconcile."""
    data = _run_gh(
        [
            "gh", "issue", "list",
            "--repo", f"{owner}/{repo}",
            "--state", "all",
            "--limit", str(limit),
            "--json", "number,title,body,state,labels,createdAt,closedAt",
        ]
    )
    out: list[GhIssue] = []
    for d in data:
        labels = tuple(label["name"] for label in d.get("labels", []) if "name" in label)
        out.append(
            GhIssue(
                repo=repo,
                number=int(d["number"]),
                title=d.get("title", "") or "",
                body=d.get("body", "") or "",
                state=d.get("state", "OPEN"),
                labels=labels,
                created_at=d.get("createdAt", "") or "",
                closed_at=d.get("closedAt"),
            )
        )
    return out


def fetch_prs(owner: str, repo: str, limit: int = 1000) -> list[GhPr]:
    """Lista PRs (todos os estados) com campos necessários pro reconcile."""
    data = _run_gh(
        [
            "gh", "pr", "list",
            "--repo", f"{owner}/{repo}",
            "--state", "all",
            "--limit", str(limit),
            "--json",
            "number,title,body,state,url,createdAt,mergedAt,headRefName,additions,deletions,changedFiles",
        ]
    )
    out: list[GhPr] = []
    for d in data:
        out.append(
            GhPr(
                repo=repo,
                number=int(d["number"]),
                title=d.get("title", "") or "",
                body=d.get("body", "") or "",
                state=d.get("state", "OPEN"),
                url=d.get("url", "") or "",
                created_at=d.get("createdAt", "") or "",
                merged_at=d.get("mergedAt"),
                head_ref_name=d.get("headRefName", "") or "",
                additions=int(d.get("additions") or 0),
                deletions=int(d.get("deletions") or 0),
                changed_files=int(d.get("changedFiles") or 0),
            )
        )
    return out
