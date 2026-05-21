"""Backfill retroativo de sessões Claude Code → tabela `agent_sessions`.

Lê transcripts em `~/.claude/projects/<encoded-path>/<uuid>.jsonl`, classifica
cada sessão (master/manual/atomic/orchestrator/unknown), cruza com GitHub
(via `gh` CLI) pra promover linkagem (HIGH/MEDIUM/LOW/ORPHAN) e popula
`agent_sessions` via UPSERT idempotente.

Características:
- **Opt-in**: invocado por comando explícito `mmb-logger backfill-agent-sessions`,
  nunca por reconcile ou cron.
- **Idempotente**: rerun não duplica linhas (UPSERT por `session_id`).
- **Aditivo**: não toca `ciclos`, `epicos`, `eventos` nem endpoints HTTP.
- **Custo explicitamente estimado**: `cost_usd_estimated` + `cost_pricing_version`
  + `cost_confidence`. NUNCA grava em `ciclos.cost_usd`.
- **`duration_api_ms` NULL retroativo**: proxy de transcript é fraca; só
  preencher quando vier de `claude -p --output-format json`.
- **ORPHAN é estado válido**: sessões Master/manual ficam ORPHAN por
  design ("sem ciclo MMB único confiável"), não erro.

Reusa de `reconcile/transcripts.py`:
- `PRICING` (tabela de preços per million tokens).
- `encode_worktree_path` (encoding determinístico cwd → dir name).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from mmb_logger.db import get_conn, init_db, now_iso
from mmb_logger.reconcile.transcripts import PRICING, encode_worktree_path
from mmb_logger.targets import load_targets, short_to_repo

# ── Constantes ────────────────────────────────────────────────────────────

PRICING_VERSION = "2026-05-16"
"""Espelhada de reconcile/transcripts.py — atualizar sincronizadamente."""

IDLE_GAP_THRESHOLD_MS = 5 * 60 * 1000
"""Gap entre turns acima desse threshold = idle (não contabilizado em active)."""

DEFAULT_CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
"""Diretório padrão de transcripts do Claude Code."""

# Encoding do path: <home>/llab/MMB → -home-eliezer-llab-MMB
# Master sempre no path raiz; atomics em --worktrees-<slug>.
# Manual = path do target sem worktree (debug interativo).
_ATOMIC_RE = re.compile(r"^(?P<prefix>.+?)-mmb-(?P<repo>[a-z]+)--worktrees-(?P<slug>.+)$")
_MANUAL_RE = re.compile(r"^(?P<prefix>.+?)-mmb-(?P<repo>[a-z]+)$")

CYCLE_KEY_RE = re.compile(r"<!--\s*mmb-cycle-key:\s*(\S+).*?-->", re.DOTALL)
"""Captura âncora 'mmb-cycle-key: <slug/proj/ts>' do body da issue."""

# Colunas escrevíveis pelo backfill. Como não há domínio humano em
# `agent_sessions` (ainda), todas as colunas derivadas estão aqui.
# Pattern espelha `DERIVED_COLS` de reconcile/reconcile.py — facilita
# evolução futura caso surjam colunas humanas.
DERIVED_COLS: tuple[str, ...] = (
    "transcript_path", "project_encoded_dir", "cwd", "git_branch",
    "role", "link_confidence", "link_reason", "evidence_json",
    "ciclo_id", "epico_id", "project", "task_id_raw", "task_id_normalized",
    "slug", "candidate_issue_number", "candidate_pr_number", "candidate_pr_url",
    "started_at", "ended_at", "duration_wall_ms", "active_interaction_ms",
    "duration_api_ms",
    "model_resolved", "models_json", "permission_mode", "claude_version",
    "has_synthetic_turns",
    "input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens",
    "cost_usd_estimated", "cost_pricing_version", "cost_confidence",
    "num_turns", "tool_call_count_total", "tool_calls_by_name_json",
    "files_read_json", "files_edited_json", "bash_commands_count",
    "ingested_at", "source",
)


# ── Modelos ───────────────────────────────────────────────────────────────


@dataclass
class SessionRecord:
    """Uma sessão Claude Code, com tudo populado pra UPSERT."""

    # Identidade
    session_id: str
    transcript_path: str
    project_encoded_dir: str | None = None
    cwd: str | None = None
    git_branch: str | None = None

    # Classificação
    role: str = "unknown"
    link_confidence: str = "ORPHAN"
    link_reason: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    # Linkagem MMB
    ciclo_id: str | None = None
    epico_id: str | None = None
    project: str | None = None
    task_id_raw: str | None = None
    task_id_normalized: str | None = None
    slug: str | None = None
    candidate_issue_number: int | None = None
    candidate_pr_number: int | None = None
    candidate_pr_url: str | None = None

    # Tempo
    started_at: str | None = None
    ended_at: str | None = None
    duration_wall_ms: int | None = None
    active_interaction_ms: int | None = None
    duration_api_ms: int | None = None  # SEMPRE None retroativo

    # Modelo / custo / tokens
    model_resolved: str | None = None
    models: dict[str, int] = field(default_factory=dict)
    permission_mode: str | None = None
    claude_version: str | None = None
    has_synthetic_turns: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd_estimated: float | None = None
    cost_pricing_version: str = PRICING_VERSION
    cost_confidence: str | None = None

    # Atividade
    num_turns: int = 0
    tool_calls_by_name: dict[str, int] = field(default_factory=dict)
    files_read: list[str] = field(default_factory=list)
    files_edited: list[str] = field(default_factory=list)
    bash_commands_count: int = 0

    # internos do parser (não persistidos)
    _cache_5m_write_tokens: int = 0
    _cache_1h_write_tokens: int = 0
    _malformed_lines: int = 0


@dataclass
class BackfillResult:
    sessions_processed: int = 0
    inserted: int = 0
    updated: int = 0
    by_role: dict[str, int] = field(default_factory=dict)
    by_confidence: dict[str, int] = field(default_factory=dict)
    cost_by_role: dict[str, float] = field(default_factory=dict)
    cost_by_confidence: dict[str, float] = field(default_factory=dict)
    cost_total_usd: float = 0.0
    top_by_cost: list[dict[str, Any]] = field(default_factory=list)
    top_by_duration: list[dict[str, Any]] = field(default_factory=list)
    top_by_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False


# ── Classificação ─────────────────────────────────────────────────────────


def classify_dir(dir_name: str, mmb_root: Path) -> tuple[str, str | None, str | None]:
    """Classifica o diretório encoded → (role, repo, slug).

    Regras (em ordem):
      1. dir == encode(mmb_root) → role=master
      2. dir == encode(mmb_root)+'-mmb-<repo>--worktrees-<slug>' → atomic
      3. dir == encode(mmb_root)+'-mmb-<repo>' (sem worktree) → manual
      4. caso contrário → unknown
    """
    master_encoded = encode_worktree_path(str(mmb_root))
    if dir_name == master_encoded:
        return "master", None, None

    # Tenta atomic: <master>-mmb-<repo>--worktrees-<slug>
    m = _ATOMIC_RE.match(dir_name)
    if m and m.group("prefix") == master_encoded:
        return "atomic", m.group("repo"), m.group("slug")

    # Tenta manual: <master>-mmb-<repo>
    m = _MANUAL_RE.match(dir_name)
    if m and m.group("prefix") == master_encoded:
        return "manual", m.group("repo"), None

    return "unknown", None, None


def extract_task_id_raw(slug: str | None) -> str | None:
    """Slug encoded → task_id heurístico (preserva forma do filesystem).

    Convenções observadas no MMB:
      - 'M7-cockpit-ui-fixes'              → 'M7'
      - 'A1-role-planner-atomic-amarelo'   → 'A1'
      - '1-1-blocos-progresso'              → '1-1'  (era '1.1' na branch)
      - '37-human-intent-instruction'      → '37'
      - 'X1-cleanup-task-scripts'          → 'X1'

    NUNCA destrói o valor original. Normalização vem em normalize_task_id.
    """
    if not slug:
        return None
    # letra+dígito (M7, A1, X1, U1, L7)
    m = re.match(r"^([A-Z]\d+)-", slug)
    if m:
        return m.group(1)
    # dois pares numéricos (1-1, 2-1)
    m = re.match(r"^(\d+-\d+)-", slug)
    if m:
        return m.group(1)
    # só dígitos prefix (37)
    m = re.match(r"^(\d+)-", slug)
    if m:
        return m.group(1)
    return None


def normalize_task_id(raw: str | None, branch: str | None) -> tuple[str | None, str]:
    """Promove task_id_raw → normalizado usando branch como evidência externa.

    Regra: o encoding do Claude Code substitui `.` por `-` no nome do dir.
    Quando a branch contém o token original (`task/1.1-...`), recuperamos
    a forma canônica. Caso contrário, raw é mantido.

    Retorna (normalized, rule_description).
    """
    if not branch or not branch.startswith("task/"):
        if raw is None:
            return None, "raw=None e branch ausente"
        return raw, "raw mantido (sem branch task/)"
    after = branch[len("task/"):]
    # Primeiro token: tudo até o primeiro '-' que NÃO faz parte de um número
    # com ponto. Usa regex pra capturar formas como 'M7', '1.1', '1.2.3'.
    m = re.match(r"^([A-Z]?\d+(?:\.\d+)*)", after)
    branch_token = m.group(1) if m else after.split("-")[0]

    if raw is None:
        return branch_token, "derivado da branch (raw=None)"
    if raw == branch_token:
        return raw, "raw já casa com branch"
    # Promoção: raw='1-1', branch_token='1.1'
    if raw.replace("-", ".") == branch_token:
        return branch_token, f"promovido: raw='{raw}' → normalized='{branch_token}' via branch"
    # Branch diverge — mantém raw, mas registra
    return raw, f"raw='{raw}' diverge de branch_token='{branch_token}'; raw preservado"


# ── Parsing de transcript ─────────────────────────────────────────────────


def parse_session_metrics(jsonl_path: Path) -> SessionRecord:
    """Lê 1 transcript JSONL e devolve `SessionRecord` parcialmente populado.

    Popula tudo que vem do próprio transcript:
    - timestamps (first/last → started/ended/duration_wall)
    - active_interaction_ms (heurística IDLE_GAP_THRESHOLD_MS)
    - usage tokens (input/output/cache_read/cache_5m/cache_1h)
    - turns counts, models, permission_mode, claude_version
    - tool_calls_by_name, files_read, files_edited, bash_commands_count
    - has_synthetic_turns (model '<synthetic>')

    Linkagem (role/repo/slug/ciclo) e custo vêm fora.
    """
    session_id = jsonl_path.stem
    rec = SessionRecord(
        session_id=session_id,
        transcript_path=str(jsonl_path),
    )

    first_ts_str: str | None = None
    last_ts_str: str | None = None
    files_read_set: set[str] = set()
    files_edited_set: set[str] = set()
    turn_timestamps: list[datetime] = []

    try:
        with jsonl_path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    rec._malformed_lines += 1
                    continue
                if not isinstance(d, dict):
                    continue

                ts = d.get("timestamp")
                if isinstance(ts, str):
                    if first_ts_str is None or ts < first_ts_str:
                        first_ts_str = ts
                    if last_ts_str is None or ts > last_ts_str:
                        last_ts_str = ts

                if rec.cwd is None and d.get("cwd"):
                    rec.cwd = d["cwd"]
                if rec.git_branch is None and d.get("gitBranch"):
                    rec.git_branch = d["gitBranch"]
                if rec.claude_version is None and d.get("version"):
                    rec.claude_version = d["version"]

                etype = d.get("type")

                if etype == "permission-mode":
                    pm = d.get("permissionMode")
                    if pm and rec.permission_mode is None:
                        rec.permission_mode = pm

                if etype in ("user", "assistant") and ts:
                    parsed = _parse_iso(ts)
                    if parsed:
                        turn_timestamps.append(parsed)
                    rec.num_turns += 1

                if etype == "assistant":
                    msg = d.get("message") or {}
                    if not isinstance(msg, dict):
                        continue
                    model = msg.get("model")
                    if isinstance(model, str) and model:
                        rec.models[model] = rec.models.get(model, 0) + 1
                        if model == "<synthetic>":
                            rec.has_synthetic_turns = True

                    usage = msg.get("usage") or {}
                    if isinstance(usage, dict):
                        rec.input_tokens += int(usage.get("input_tokens") or 0)
                        rec.output_tokens += int(usage.get("output_tokens") or 0)
                        rec.cache_read_tokens += int(usage.get("cache_read_input_tokens") or 0)
                        cc = usage.get("cache_creation") or {}
                        if isinstance(cc, dict):
                            rec._cache_5m_write_tokens += int(
                                cc.get("ephemeral_5m_input_tokens") or 0
                            )
                            rec._cache_1h_write_tokens += int(
                                cc.get("ephemeral_1h_input_tokens") or 0
                            )
                        else:
                            # fallback: top-level cache_creation_input_tokens → 5m
                            rec._cache_5m_write_tokens += int(
                                usage.get("cache_creation_input_tokens") or 0
                            )

                    # Tool calls
                    content = msg.get("content")
                    if isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") != "tool_use":
                                continue
                            name = block.get("name") or "?"
                            rec.tool_calls_by_name[name] = (
                                rec.tool_calls_by_name.get(name, 0) + 1
                            )
                            inp = block.get("input") or {}
                            if not isinstance(inp, dict):
                                continue
                            if name == "Read" and inp.get("file_path"):
                                files_read_set.add(inp["file_path"])
                            elif name in ("Edit", "Write") and inp.get("file_path"):
                                files_edited_set.add(inp["file_path"])
                            elif name == "Bash":
                                rec.bash_commands_count += 1
    except OSError:
        # arquivo ilegível — devolve registro mínimo, caller decide warning
        pass

    rec.started_at = first_ts_str
    rec.ended_at = last_ts_str
    rec.duration_wall_ms = _duration_ms(first_ts_str, last_ts_str)
    rec.active_interaction_ms = _active_interaction(turn_timestamps)
    # files cap em 200 cada pra não inflar a tabela com sessões master gigantes
    rec.files_read = sorted(files_read_set)[:200]
    rec.files_edited = sorted(files_edited_set)[:200]

    # cache_creation_tokens persistido é a soma de 5m+1h (granularidade
    # interna do parser não interessa pro consumidor)
    rec.cache_creation_tokens = rec._cache_5m_write_tokens + rec._cache_1h_write_tokens

    # Model dominante
    if rec.models:
        rec.model_resolved = max(rec.models.items(), key=lambda kv: kv[1])[0]

    return rec


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _duration_ms(first: str | None, last: str | None) -> int | None:
    a = _parse_iso(first)
    b = _parse_iso(last)
    if not a or not b:
        return None
    delta_ms = int((b - a).total_seconds() * 1000)
    return delta_ms if delta_ms >= 0 else None


def _active_interaction(turn_timestamps: list[datetime]) -> int | None:
    """Soma de gaps entre turns consecutivos, ignorando gaps > IDLE_GAP_THRESHOLD_MS."""
    if len(turn_timestamps) < 2:
        return None
    sorted_ts = sorted(turn_timestamps)
    active = 0
    for i in range(1, len(sorted_ts)):
        dt_ms = int((sorted_ts[i] - sorted_ts[i - 1]).total_seconds() * 1000)
        if 0 < dt_ms < IDLE_GAP_THRESHOLD_MS:
            active += dt_ms
    return active


# ── Custo ─────────────────────────────────────────────────────────────────


def compute_cost(rec: SessionRecord) -> tuple[float | None, str]:
    """Devolve (cost_usd_estimated, cost_confidence).

    `cost_confidence`:
    - 'no_model'         — nenhum model identificado
    - 'unknown_model'    — model fora da PRICING
    - 'has_synthetic'    — sessão tem turns <synthetic>
    - 'mixed_models'     — múltiplos models na mesma sessão
    - 'ok_unvalidated'   — caminho mais confiável (sem validação contra Console)
    """
    if not rec.model_resolved:
        return None, "no_model"
    if rec.model_resolved not in PRICING:
        return None, "unknown_model"
    p = PRICING[rec.model_resolved]
    cost = (
        rec.input_tokens * p["input"]
        + rec.output_tokens * p["output"]
        + rec._cache_5m_write_tokens * p["cache_5m_write"]
        + rec._cache_1h_write_tokens * p["cache_1h_write"]
        + rec.cache_read_tokens * p["cache_read"]
    ) / 1_000_000.0
    cost_rounded = round(cost, 6)
    if rec.has_synthetic_turns:
        return cost_rounded, "has_synthetic"
    if len(rec.models) > 1:
        return cost_rounded, "mixed_models"
    return cost_rounded, "ok_unvalidated"


# ── Linkagem GitHub ───────────────────────────────────────────────────────


def _fetch_prs_with_closing(owner: str, repo: str) -> dict[str, dict[str, Any]]:
    """gh pr list incluindo closingIssuesReferences. Indexa por headRefName.

    Não reusa `reconcile/gh.py::fetch_prs` porque o GhPr dataclass de lá
    não inclui closingIssuesReferences (Categoria A: não tocar gh.py pra
    não mudar contrato público da função existente).

    Retorna mapa headRefName → dict bruto do gh. Vazio se gh falhar.
    """
    if not shutil.which("gh"):
        return {}
    cmd = [
        "gh", "pr", "list",
        "--repo", f"{owner}/{repo}",
        "--state", "all", "--limit", "300",
        "--json",
        "number,headRefName,state,mergedAt,closingIssuesReferences,labels,title,url",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        return {}
    if res.returncode != 0:
        return {}
    try:
        data = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, list):
        return {}
    return {pr["headRefName"]: pr for pr in data if isinstance(pr, dict)}


def _fetch_issue_body(owner: str, repo: str, number: int) -> dict[str, Any] | None:
    if not shutil.which("gh"):
        return None
    cmd = [
        "gh", "issue", "view", str(number),
        "--repo", f"{owner}/{repo}",
        "--json", "body,labels,state",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if res.returncode != 0:
        return None
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return None


def extract_cycle_key(issue_body: str | None) -> str | None:
    if not issue_body:
        return None
    m = CYCLE_KEY_RE.search(issue_body)
    return m.group(1).strip() if m else None


def link_via_github(
    rec: SessionRecord,
    *,
    prs_by_target: dict[str, dict[str, dict[str, Any]]],
    issues_cache: dict[tuple[str, int], dict[str, Any] | None],
    owner_by_repo: dict[str, str],
) -> None:
    """Muta `rec` populando linkagem GH e confiança.

    - role != atomic              → ORPHAN
    - atomic + sem branch/repo    → LOW
    - atomic + PR achado + âncora → HIGH (com ciclo_key)
    - atomic + PR achado, sem âncora → MEDIUM
    - atomic + sem PR             → LOW
    """
    if rec.role != "atomic":
        rec.link_confidence = "ORPHAN"
        rec.link_reason = f"role={rec.role}; ORPHAN não é erro, é estado válido"
        return

    repo_short = rec.project
    target_full = short_to_repo(repo_short) if repo_short else None
    branch = rec.git_branch

    if not target_full or not branch or target_full not in prs_by_target:
        rec.link_confidence = "LOW"
        rec.link_reason = "atomic sem repo/branch resolvíveis em targets"
        return

    pr = prs_by_target[target_full].get(branch)
    if not pr:
        rec.link_confidence = "LOW"
        rec.link_reason = (
            f"atomic: branch '{branch}' não casou nenhum PR em {target_full}"
        )
        return

    rec.candidate_pr_number = int(pr.get("number") or 0) or None
    rec.candidate_pr_url = pr.get("url") or None
    rec.evidence["pr_state"] = pr.get("state")
    rec.evidence["pr_merged_at"] = pr.get("mergedAt")
    labels = [
        lab.get("name") for lab in (pr.get("labels") or [])
        if isinstance(lab, dict) and lab.get("name")
    ]
    if labels:
        rec.evidence["pr_labels"] = labels

    closing = pr.get("closingIssuesReferences") or []
    if closing and isinstance(closing[0], dict):
        issue_n = closing[0].get("number")
        if isinstance(issue_n, int) and issue_n > 0:
            rec.candidate_issue_number = issue_n
            cache_key = (target_full, issue_n)
            if cache_key not in issues_cache:
                owner = owner_by_repo.get(target_full, "x-force-42")
                issues_cache[cache_key] = _fetch_issue_body(owner, target_full, issue_n)
            issue_data = issues_cache[cache_key]
            if issue_data:
                ck = extract_cycle_key(issue_data.get("body"))
                if ck:
                    rec.evidence["mmb_cycle_key"] = ck
                    rec.evidence["issue_has_anchor"] = True
                else:
                    rec.evidence["issue_has_anchor"] = False
                issue_labels = [
                    lab.get("name") for lab in (issue_data.get("labels") or [])
                    if isinstance(lab, dict) and lab.get("name")
                ]
                if issue_labels:
                    rec.evidence["issue_labels"] = issue_labels

    if rec.evidence.get("issue_has_anchor"):
        rec.link_confidence = "HIGH"
        rec.link_reason = (
            f"PR #{rec.candidate_pr_number} + issue "
            f"#{rec.candidate_issue_number} com âncora mmb-cycle-key"
        )
    else:
        rec.link_confidence = "MEDIUM"
        rec.link_reason = (
            f"PR #{rec.candidate_pr_number} achado por branch '{branch}', "
            f"issue sem âncora (provável ciclo legacy ou atomic pré-âncora)"
        )


# ── Linkagem com ciclos/épicos no DB ──────────────────────────────────────


def link_to_db_ciclo(rec: SessionRecord, conn) -> None:
    """Tenta resolver `ciclo_id` e `epico_id` no DB local pra atomics HIGH/MEDIUM.

    Estratégia (best-effort, sem quebrar ORPHAN):
    - Se temos `candidate_pr_number` e `project`: SELECT ciclo via pr_number.
    - Se achou ciclo: também popula `epico_id` via FK.
    - Caso contrário: NULL (não é erro).
    """
    if rec.role != "atomic" or not rec.candidate_pr_number or not rec.project:
        return
    repo = short_to_repo(rec.project)
    legacy = f"mmb-{rec.project}"
    candidates = (repo,) if repo == legacy else (repo, legacy)
    placeholders = ",".join("?" * len(candidates))
    row = conn.execute(
        f"SELECT id, epico_id FROM ciclos "
        f"WHERE project IN ({placeholders}) AND pr_number = ?",
        (*candidates, rec.candidate_pr_number),
    ).fetchone()
    if row:
        rec.ciclo_id = row["id"]
        rec.epico_id = row["epico_id"]


# ── Discovery de transcripts ──────────────────────────────────────────────


def discover_sessions(
    *,
    mmb_root: Path,
    claude_projects: Path,
) -> list[Path]:
    """Lista todos os .jsonl em dirs encoded sob o mmb_root.

    Retorna paths absolutos, ordenados pra determinismo.
    """
    if not claude_projects.is_dir():
        return []
    master_encoded = encode_worktree_path(str(mmb_root))
    results: list[Path] = []
    for d in claude_projects.iterdir():
        if not d.is_dir():
            continue
        if d.name != master_encoded and not d.name.startswith(master_encoded + "-"):
            continue
        results.extend(sorted(d.glob("*.jsonl")))
    return sorted(results)


# ── UPSERT idempotente ────────────────────────────────────────────────────


def _record_to_row(rec: SessionRecord) -> dict[str, Any]:
    return {
        "session_id": rec.session_id,
        "transcript_path": rec.transcript_path,
        "project_encoded_dir": rec.project_encoded_dir,
        "cwd": rec.cwd,
        "git_branch": rec.git_branch,
        "role": rec.role,
        "link_confidence": rec.link_confidence,
        "link_reason": rec.link_reason,
        "evidence_json": json.dumps(rec.evidence, ensure_ascii=False) if rec.evidence else None,
        "ciclo_id": rec.ciclo_id,
        "epico_id": rec.epico_id,
        "project": rec.project,
        "task_id_raw": rec.task_id_raw,
        "task_id_normalized": rec.task_id_normalized,
        "slug": rec.slug,
        "candidate_issue_number": rec.candidate_issue_number,
        "candidate_pr_number": rec.candidate_pr_number,
        "candidate_pr_url": rec.candidate_pr_url,
        "started_at": rec.started_at,
        "ended_at": rec.ended_at,
        "duration_wall_ms": rec.duration_wall_ms,
        "active_interaction_ms": rec.active_interaction_ms,
        "duration_api_ms": rec.duration_api_ms,  # SEMPRE None retroativo
        "model_resolved": rec.model_resolved,
        "models_json": json.dumps(rec.models, ensure_ascii=False) if rec.models else None,
        "permission_mode": rec.permission_mode,
        "claude_version": rec.claude_version,
        "has_synthetic_turns": 1 if rec.has_synthetic_turns else 0,
        "input_tokens": rec.input_tokens,
        "output_tokens": rec.output_tokens,
        "cache_creation_tokens": rec.cache_creation_tokens,
        "cache_read_tokens": rec.cache_read_tokens,
        "cost_usd_estimated": rec.cost_usd_estimated,
        "cost_pricing_version": rec.cost_pricing_version,
        "cost_confidence": rec.cost_confidence,
        "num_turns": rec.num_turns,
        "tool_call_count_total": sum(rec.tool_calls_by_name.values()) or 0,
        "tool_calls_by_name_json": (
            json.dumps(rec.tool_calls_by_name, ensure_ascii=False)
            if rec.tool_calls_by_name else None
        ),
        "files_read_json": (
            json.dumps(rec.files_read, ensure_ascii=False) if rec.files_read else None
        ),
        "files_edited_json": (
            json.dumps(rec.files_edited, ensure_ascii=False) if rec.files_edited else None
        ),
        "bash_commands_count": rec.bash_commands_count,
        "ingested_at": now_iso(),
        "source": "claude_transcript_backfill",
    }


def upsert_session(conn, rec: SessionRecord) -> str:
    """Insere ou atualiza. Retorna 'inserted' ou 'updated'.

    UPSERT seletivo: ON CONFLICT atualiza só colunas em DERIVED_COLS.
    Pattern espelha `reconcile/reconcile.py` pra deixar espaço a futuras
    colunas humanas em `agent_sessions`.
    """
    row = _record_to_row(rec)
    existed = conn.execute(
        "SELECT 1 FROM agent_sessions WHERE session_id = ?", (rec.session_id,)
    ).fetchone()
    all_cols = ["session_id", *DERIVED_COLS]
    placeholders = ",".join("?" for _ in all_cols)
    update_setters = ",".join(f"{c}=excluded.{c}" for c in DERIVED_COLS)
    sql = (
        f"INSERT INTO agent_sessions ({','.join(all_cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(session_id) DO UPDATE SET {update_setters}"
    )
    values = [row[c] for c in all_cols]
    conn.execute(sql, values)
    return "updated" if existed else "inserted"


# ── Pipeline principal ────────────────────────────────────────────────────


def build_owner_map() -> dict[str, str]:
    """Carrega targets.json → mapa repo (mmb-cockpit) → owner."""
    out: dict[str, str] = {}
    try:
        for t in load_targets():
            if t.owner:
                out[t.repo] = t.owner
    except Exception:
        pass
    # Fallback histórico: core não está no registry mas existe em path antigo
    out.setdefault("mmb-core", "x-force-42")
    return out


def collect_prs_for_all_targets(
    owner_by_repo: dict[str, str],
    extra_repos: tuple[str, ...] = ("mmb-core",),
) -> dict[str, dict[str, dict[str, Any]]]:
    """Retorna {target_full: {headRefName: pr_dict}} pra todos os repos relevantes.

    Inclui 'mmb-core' como histórico (não está em targets.json).
    """
    out: dict[str, dict[str, dict[str, Any]]] = {}
    repos = sorted(set(owner_by_repo.keys()) | set(extra_repos))
    for repo in repos:
        owner = owner_by_repo.get(repo, "x-force-42")
        out[repo] = _fetch_prs_with_closing(owner, repo)
    return out


def process_all(
    *,
    mmb_root: Path,
    claude_projects: Path,
    no_gh: bool = False,
    limit: int | None = None,
) -> list[SessionRecord]:
    """Pipeline read-only: descobre, parseia, classifica, linka.

    Não toca DB. Retorna a lista de SessionRecord pronta pra UPSERT.
    """
    jsonl_paths = discover_sessions(mmb_root=mmb_root, claude_projects=claude_projects)
    if limit is not None:
        jsonl_paths = jsonl_paths[:limit]

    owner_by_repo = build_owner_map()
    prs_by_target: dict[str, dict[str, dict[str, Any]]] = (
        {} if no_gh else collect_prs_for_all_targets(owner_by_repo)
    )
    issues_cache: dict[tuple[str, int], dict[str, Any] | None] = {}

    records: list[SessionRecord] = []
    for jsonl_path in jsonl_paths:
        rec = parse_session_metrics(jsonl_path)
        rec.project_encoded_dir = jsonl_path.parent.name
        role, repo, slug = classify_dir(jsonl_path.parent.name, mmb_root)
        rec.role = role
        rec.project = repo
        rec.slug = slug
        rec.task_id_raw = extract_task_id_raw(slug)
        normalized, rule = normalize_task_id(rec.task_id_raw, rec.git_branch)
        rec.task_id_normalized = normalized
        if rule:
            rec.evidence["task_id_norm_rule"] = rule

        # Custo
        rec.cost_usd_estimated, rec.cost_confidence = compute_cost(rec)

        # Linkagem GH (ou pula em modo offline)
        if no_gh:
            if rec.role == "atomic":
                rec.link_confidence = "LOW"
                rec.link_reason = "atomic sem cross-GH (--no-gh)"
            else:
                rec.link_confidence = "ORPHAN"
                rec.link_reason = f"role={rec.role}; --no-gh"
        else:
            link_via_github(
                rec,
                prs_by_target=prs_by_target,
                issues_cache=issues_cache,
                owner_by_repo=owner_by_repo,
            )

        records.append(rec)
    return records


def write_records(conn, records: list[SessionRecord]) -> tuple[int, int]:
    """UPSERT em lote. Retorna (inserted, updated)."""
    inserted = updated = 0
    for rec in records:
        link_to_db_ciclo(rec, conn)  # tenta resolver ciclo_id no DB
        outcome = upsert_session(conn, rec)
        if outcome == "inserted":
            inserted += 1
        else:
            updated += 1
    return inserted, updated


def export_jsonl(records: list[SessionRecord], path: Path) -> None:
    """Exporta dataset pra inspeção. Não persiste no DB."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            row = _record_to_row(rec)
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def summarize(records: list[SessionRecord]) -> BackfillResult:
    """Agrega counters e tops pra retorno do comando."""
    result = BackfillResult(sessions_processed=len(records))
    result.by_role = dict(Counter(r.role for r in records))
    result.by_confidence = dict(Counter(r.link_confidence for r in records))
    cost_role: dict[str, float] = defaultdict(float)
    cost_conf: dict[str, float] = defaultdict(float)
    for r in records:
        if r.cost_usd_estimated is not None:
            cost_role[r.role] += r.cost_usd_estimated
            cost_conf[r.link_confidence] += r.cost_usd_estimated
            result.cost_total_usd += r.cost_usd_estimated
    result.cost_by_role = dict(cost_role)
    result.cost_by_confidence = dict(cost_conf)

    # Tops
    def _topinfo(r: SessionRecord, key: str, val: Any) -> dict[str, Any]:
        return {
            "session_id": r.session_id[:8],
            "role": r.role,
            "project": r.project,
            "slug": r.slug,
            key: val,
        }

    result.top_by_cost = [
        _topinfo(r, "cost_usd_estimated", round(r.cost_usd_estimated or 0, 4))
        for r in sorted(records, key=lambda x: x.cost_usd_estimated or 0, reverse=True)[:10]
    ]
    result.top_by_duration = [
        _topinfo(r, "duration_wall_ms", r.duration_wall_ms or 0)
        for r in sorted(records, key=lambda x: x.duration_wall_ms or 0, reverse=True)[:10]
    ]
    result.top_by_tool_calls = [
        _topinfo(r, "tool_call_count_total", sum(r.tool_calls_by_name.values()))
        for r in sorted(
            records, key=lambda x: sum(x.tool_calls_by_name.values()), reverse=True
        )[:10]
    ]
    return result


# ── Entry point ───────────────────────────────────────────────────────────


def backfill_agent_sessions(
    *,
    db_path: str | os.PathLike[str] | None = None,
    mmb_root: str | os.PathLike[str] | None = None,
    claude_projects: str | os.PathLike[str] | None = None,
    write: bool = False,
    dry_run: bool = False,
    no_gh: bool = False,
    limit: int | None = None,
    output_jsonl: str | os.PathLike[str] | None = None,
) -> BackfillResult:
    """Entry point principal — chamado pelo CLI ou por testes.

    Comportamento:
    - `write=True` e `dry_run=False`: persiste no DB.
    - `dry_run=True`: lê e agrega, mas não escreve no DB.
    - `dry_run=True` e `write=True`: erro (mutuamente exclusivos).
    - Sem nenhum dos dois: erro (fail-safe; CLI mostra ajuda).
    """
    if write and dry_run:
        raise ValueError("--write e --dry-run são mutuamente exclusivos")
    if not write and not dry_run:
        raise ValueError(
            "Especifique --dry-run (simulação) ou --write (persistir). "
            "Sem nenhum dos dois, o comando não roda por fail-safe."
        )

    mmb_root_p = _resolve_mmb_root(mmb_root)
    claude_projects_p = (
        Path(claude_projects).expanduser() if claude_projects else DEFAULT_CLAUDE_PROJECTS
    )

    records = process_all(
        mmb_root=mmb_root_p,
        claude_projects=claude_projects_p,
        no_gh=no_gh,
        limit=limit,
    )

    if output_jsonl:
        export_jsonl(records, Path(output_jsonl))

    result = summarize(records)
    result.dry_run = dry_run

    if write:
        init_db(db_path)  # garante que a tabela existe
        with get_conn(db_path) as conn:
            inserted, updated = write_records(conn, records)
        result.inserted = inserted
        result.updated = updated

    return result


def _resolve_mmb_root(explicit: str | os.PathLike[str] | None) -> Path:
    """Resolve MMB root: arg > env MMB_ROOT > walk-up procurando .tooling/."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("MMB_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    # Walk-up a partir de cwd procurando .tooling/targets.json
    here = Path.cwd().resolve()
    for ancestor in (here, *here.parents):
        if (ancestor / ".tooling" / "targets.json").is_file():
            return ancestor
    raise FileNotFoundError(
        "MMB root não encontrado. Use --mmb-root ou defina MMB_ROOT."
    )
