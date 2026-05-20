"""Reconciler do mmb-logger (fase 1 + fase 2).

Projeta o estado canônico do método em rows de `ciclos` e `epicos`:
- Fase 1: GitHub issues + PRs → planejado / pr_aberto / completo /
  abortado-pós-GH.
- Fase 2: dispatch master→planner em `inbox/` → `iniciado`. Casamento
  briefing ↔ issue via âncora `mmb-cycle-key`. Aborto pré-GH a partir
  de sinais colaterais (`commd-worker-exit/timeout`, `agents.jsonl`
  deregister, stale threshold).

Preserva colunas humanas (`assertiveness_score`, `review_note`) via
UPSERT seletivo. Idempotente: rodar duas vezes seguidas produz estado
idêntico.

`--reset` é DESTRUTIVO: apaga ciclos + épicos antes de reconciliar;
eventos órfãos caem via ON DELETE CASCADE; anotações humanas cascateiam
junto. Use SOMENTE em cutover/rebuild controlado, com snapshot prévio
do DB. Operação normal é reconcile aditivo (sem --reset).
"""

from __future__ import annotations

import sqlite3
import sys
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from mmb_logger.db import get_conn, upsert_projeto
from mmb_logger.reconcile._runtime import resolve_andaime_version, resolve_tooling_root
from mmb_logger.reconcile.abort import (
    AbortSignal,
    detect_abort_signal,
    load_agent_deregister_signals,
    load_journal_worker_signals,
    resolve_stale_threshold_s,
)
from mmb_logger.reconcile.audit import AuditCounts, write_audit_events
from mmb_logger.reconcile.derive import (
    epic_from_labels,
    parse_anchor,
    parse_closes,
)
from mmb_logger.reconcile.gh import REPOS, GhIssue, GhPr, fetch_issues, fetch_prs
from mmb_logger.reconcile.inbox import Briefing, BriefingsLoaded, load_briefings
from mmb_logger.reconcile.intents import (
    load_archived_briefing,
    load_briefing_text,
    load_intent_text,
    parse_closed_marker,
)
from mmb_logger.reconcile.planner_models import load_planner_models
from mmb_logger.reconcile.transcripts import CostResult, compute_cost_for_ciclo
from mmb_logger.targets import Target, load_targets

OWNER_DEFAULT = "x-force-42"

# Colunas que o reconciler escreve. Tudo fora desta lista é domínio
# humano ou outras fases — UPSERT preserva.
DERIVED_COLS = (
    # Fase 1 — GitHub:
    "status",
    "pr_url",
    "pr_number",
    "merged_to_main",
    "closed_partial_at",
    "closed_complete_at",
    "diff_added",
    "diff_deleted",
    "diff_files",
    "andaime_version",
    # Fase 2 — briefing + aborto pré-GH:
    "briefing_md",
    "abort_at",
    "abort_origin",
    "abort_reason",
    # Fase 4 — custo via transcripts:
    "cost_usd",
    "tokens_input",
    "tokens_output",
    # Fase 5 — captura de modelo Claude do planner (agents.jsonl spawn):
    "model",
)


@dataclass
class ReconcileResult:
    epicos_upserted: int = 0
    ciclos_upserted: int = 0
    audit: AuditCounts = field(default_factory=AuditCounts)
    warnings: list[str] = field(default_factory=list)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        print(f"[reconcile:warn] {msg}", file=sys.stderr)


def _project_short(repo: str) -> str:
    return repo.removeprefix("mmb-")


def _projeto_id_from_target(target: Target) -> str:
    """Convenção: id/slug do projeto = `target.repo`.

    Para internos isso é `mmb-<id>` (ex.: `mmb-cockpit`); para externos
    é o próprio `<id>` (ex.: `campo-premiado`). Reflete o que já existe
    em `projetos` (mmb-cockpit, mmb-aquarium, mmb-core).
    """
    return target.repo


def _projeto_name_from_repo(repo: str) -> str:
    """`mmb-cockpit` → `MMB Cockpit`; `campo-premiado` → `Campo Premiado`."""
    if repo.startswith("mmb-"):
        rest = repo[len("mmb-"):]
        return "MMB " + " ".join(w.capitalize() for w in rest.split("-") if w)
    return " ".join(w.capitalize() for w in repo.split("-") if w)


def _projeto_repo_url(target: Target, default_owner: str) -> str:
    owner = target.owner or default_owner
    return f"git@github.com:{owner}/{target.repo}.git"


def _backfill_external_project_prefixes(
    conn: sqlite3.Connection,
    targets: list[Target],
) -> int:
    """Backfill idempotente: corrige `ciclos.project` herdado da convenção antiga
    (`mmb-<id>` para todo mundo) em targets externos cujo `repo` não tem prefixo
    `mmb-`. Roda 2x sem efeito — segundo run não encontra mais matches.
    """
    count = 0
    for t in targets:
        if t.repo.startswith("mmb-"):
            continue
        cursor = conn.execute(
            "UPDATE ciclos SET project = ? WHERE project = ?",
            (t.repo, f"mmb-{t.repo}"),
        )
        count += cursor.rowcount
    return count


def _sync_projetos_from_targets(
    conn: sqlite3.Connection,
    targets: list[Target],
    *,
    default_owner: str,
) -> int:
    """UPSERT projetos a partir do registry de targets.

    Idempotente (delega em `upsert_projeto`, que usa ON CONFLICT). Não
    deleta entries existentes — preserva histórico (ex.: `mmb-core` com
    ciclos arquivados).
    """
    count = 0
    for t in targets:
        projeto_id = _projeto_id_from_target(t)
        upsert_projeto(
            conn,
            id=projeto_id,
            slug=projeto_id,
            name=_projeto_name_from_repo(t.repo),
            path=t.local_path,
            repo_url=_projeto_repo_url(t, default_owner),
        )
        count += 1
    return count


def derive_status_with_briefing(
    briefing: Briefing | None,
    issue: GhIssue | None,
    pr: GhPr | None,
    abort_signal: AbortSignal | None,
) -> str:
    """Status canônico considerando briefing + GH + sinais de aborto pré-GH.

    Tabela:
      M=pr.merged_at → completo
      I=OPEN + P=None → planejado (com briefing) ou planejado (sem briefing)
      I=OPEN + P=existe → pr_aberto
      I=CLOSED + ¬M → abortado (pós-GH)
      sem I, briefing existe, sem abort_signal → iniciado
      sem I, briefing existe, com abort_signal → abortado (pré-GH)
    """
    if pr is not None and pr.merged_at:
        return "completo"
    if issue is not None:
        if issue.state == "OPEN":
            return "planejado" if pr is None else "pr_aberto"
        return "abortado"  # issue CLOSED sem merge
    # Sem issue — fase 2 puro
    if briefing is not None and abort_signal is not None:
        return "abortado"
    if briefing is not None:
        return "iniciado"
    # Sem briefing nem issue: caller não deveria ter chegado aqui.
    raise ValueError("derive_status: sem briefing nem issue — caso inválido")


def link_pr_to_issue(prs: list[GhPr], result: ReconcileResult) -> dict[int, GhPr]:
    """Mapeia issue.number → PR. Múltiplos PRs pra mesma issue: warn + escolhe.

    Política: PR mergeado vence. Senão, PR criado mais recentemente.
    """
    by_issue: dict[int, GhPr] = {}
    seen_multi: set[int] = set()
    for pr in prs:
        for issue_num in parse_closes(pr.body):
            existing = by_issue.get(issue_num)
            if existing is None:
                by_issue[issue_num] = pr
                continue
            if issue_num not in seen_multi:
                seen_multi.add(issue_num)
                result.warn(
                    f"multiple-prs-for-issue: issue #{issue_num} linkada por "
                    f"#{existing.number} e #{pr.number}"
                )
            if pr.merged_at and not existing.merged_at:
                by_issue[issue_num] = pr
            elif (not existing.merged_at) and pr.created_at > existing.created_at:
                by_issue[issue_num] = pr
    return by_issue


def make_cycle_id(
    epic_slug: str,
    project_short: str,
    anchor_ts: str | None,
    fallback_ts: str,
) -> str:
    """Natural key do ciclo. Prefere anchor_ts (briefing_created_ts)."""
    ts = anchor_ts or fallback_ts
    return f"{epic_slug}__{project_short}__{ts}"


def reconcile(
    db_path: str | None = None,
    owner: str = OWNER_DEFAULT,
    reset: bool = False,
    fetch_issues_fn: Callable[..., list[GhIssue]] | None = None,
    fetch_prs_fn: Callable[..., list[GhPr]] | None = None,
    repos: tuple[str, ...] = REPOS,
    andaime_version_fn: Callable[[], str | None] | None = None,
    # Fase 2 inputs — injetáveis pra teste:
    tooling_root: str | Path | None = None,
    briefings: list[Briefing] | None = None,
    briefings_malformed: list[str] | None = None,
    journal_signals: list | None = None,
    agent_signals: list | None = None,
    now_epoch: float | None = None,
    stale_threshold_s: int | None = None,
    # Fase 4 inputs — injetáveis pra teste:
    claude_projects_root: str | Path | None = None,
    mmb_root: str | Path | None = None,
    # Fase 5 input — captura de modelo do planner (agents.jsonl):
    planner_models: dict[tuple[str, str], str] | None = None,
    # Sync de projetos a partir do registry de targets:
    targets_for_sync: list[Target] | None = None,
) -> ReconcileResult:
    """Roda o reconcile completo (fase 1 + fase 2).

    Args principais (fase 1):
        db_path: SQLite. Default: env MMB_LOGGER_DB_PATH ou ./mmb-logger.db.
        owner: GH org. Default: x-force-42.
        reset: DESTRUTIVO. Apaga ciclos + epicos antes.
        fetch_issues_fn / fetch_prs_fn: injeção pra teste.
        repos: tupla de repos a varrer.
        andaime_version_fn: injeção pra teste.

    Args fase 2 (também injetáveis):
        tooling_root: raiz do `.tooling/`. Default: resolver via env.
        briefings: lista de Briefing. None → carrega de inbox via FS.
        briefings_malformed: paths de briefings malformados. None → FS.
        journal_signals / agent_signals: sinais de falha. None → FS.
        now_epoch: timestamp atual em segundos. None → time.time().
        stale_threshold_s: threshold para "stale". None → env ou default.
    """
    fi = fetch_issues_fn or fetch_issues
    fp = fetch_prs_fn or fetch_prs

    # Owner per-repo via registry (PR 2B). Repos não presentes no registry
    # (ex.: fixtures históricas como mmb-core nos testes) caem no `owner`
    # global passado como arg — backward compat preservada.
    try:
        _owner_by_repo: dict[str, str] = {
            t.repo: (t.owner or owner) for t in load_targets()
        }
    except Exception:
        _owner_by_repo = {}

    if andaime_version_fn is not None:
        version = andaime_version_fn()
    else:
        try:
            version = resolve_andaime_version(resolve_tooling_root())
        except Exception:
            version = None

    # Fase 2 inputs — carrega do FS se não injetados.
    if tooling_root is None:
        try:
            tooling_root = resolve_tooling_root()
        except Exception:
            tooling_root = None
    tooling_path = Path(tooling_root) if tooling_root else None

    if briefings is None or briefings_malformed is None:
        if tooling_path is not None:
            loaded: BriefingsLoaded = load_briefings(tooling_path)
        else:
            loaded = BriefingsLoaded(briefings=[], malformed_paths=[])
        if briefings is None:
            briefings = loaded.briefings
        if briefings_malformed is None:
            briefings_malformed = loaded.malformed_paths

    if journal_signals is None:
        journal_signals = (
            load_journal_worker_signals(tooling_path) if tooling_path else []
        )
    if agent_signals is None:
        agent_signals = (
            load_agent_deregister_signals(tooling_path) if tooling_path else []
        )

    if now_epoch is None:
        now_epoch = time.time()
    threshold = resolve_stale_threshold_s(stale_threshold_s)

    if planner_models is None:
        planner_models = (
            load_planner_models(tooling_path) if tooling_path else {}
        )

    # Fase 4: root pros transcripts e mmb_root pra construir worktree path.
    # Defaults derivam de tooling_root (mmb_root = .tooling/..) e $HOME.
    if claude_projects_root is None:
        claude_projects_root = Path.home() / ".claude" / "projects"
    else:
        claude_projects_root = Path(claude_projects_root)
    if mmb_root is None:
        mmb_root = tooling_path.parent if tooling_path else None
    else:
        mmb_root = Path(mmb_root)

    result = ReconcileResult()

    # Warnings de briefings malformados — emitidos uma vez no início.
    for p in briefings_malformed:
        result.warn(f"briefing-malformed: {p}")

    # Resolve targets a sincronizar com tabela `projetos`. None → carrega
    # do registry (`.tooling/targets.json`) e filtra por tracked_by_logger.
    if targets_for_sync is None:
        try:
            targets_for_sync = [t for t in load_targets() if t.tracked_by_logger]
        except Exception as exc:
            result.warn(f"targets-load-failed-for-projetos-sync: {exc}")
            targets_for_sync = []

    with get_conn(db_path) as conn:
        if reset:
            _reset_derived_state(conn)

        # Sincroniza tabela `projetos` com o registry — roda antes das
        # fases que projetam ciclos (preserva semântica de "projeto existe
        # antes do ciclo referenciar"). Idempotente, aditivo: não deleta
        # entries obsoletas (ex.: `mmb-core`) — preserva histórico.
        _sync_projetos_from_targets(conn, targets_for_sync, default_owner=owner)

        # L10 (logger-project-id-normalization): corrige prefix indevido
        # em ciclos legados de targets externos.
        _backfill_external_project_prefixes(conn, targets_for_sync)

        for repo in repos:
            project_short = _project_short(repo)

            repo_owner = _owner_by_repo.get(repo, owner)
            try:
                issues = fi(repo_owner, repo)
                prs = fp(repo_owner, repo)
            except Exception as exc:
                result.warn(f"gh-fetch-failed: repo={repo} err={exc}")
                continue

            pr_by_issue = link_pr_to_issue(prs, result)

            # PRs órfãos (sem Closes #N) — só warn
            for pr in prs:
                if not parse_closes(pr.body):
                    result.warn(
                        f"pr-without-closes: {repo}#{pr.number} sem Closes #N no body"
                    )

            # Briefings filtrados pra este projeto
            briefings_here = [b for b in briefings if b.project_short == project_short]
            briefings_by_key = {b.cycle_key: b for b in briefings_here}
            matched_briefing_paths: set[str] = set()

            # 1. Processa cada issue, casando com briefing via âncora
            for issue in issues:
                _process_issue(
                    conn,
                    issue,
                    pr_by_issue.get(issue.number),
                    briefings_by_key,
                    matched_briefing_paths,
                    project_short,
                    version,
                    result,
                    mmb_root=mmb_root,
                    claude_projects_root=claude_projects_root,
                    planner_models=planner_models,
                )

            # 2. Briefings não-casados → iniciado ou abortado pré-GH
            unmatched_briefings = [
                b for b in briefings_here if b.path not in matched_briefing_paths
            ]
            _process_unmatched_briefings(
                conn,
                unmatched_briefings,
                journal_signals,
                agent_signals,
                now_epoch,
                threshold,
                version,
                result,
                repo=repo,
                planner_models=planner_models,
            )

        # 3. Audit events (journal + agents + inbox) — fase 3.
        #    Roda APÓS ciclos/épicos estarem em estado final pra linkagem ter
        #    chance de casar. INSERT OR IGNORE via source_key garante idempotência.
        if tooling_path is not None:
            result.audit = write_audit_events(conn, tooling_path, result.warn)

        # 4. Enriquecimento de epicos.intencao a partir de
        #    .tooling/intents/<slug>/master-briefing.md — fase 3.
        if tooling_path is not None:
            _enrich_epicos_intencao(conn, tooling_path)

        # 5. Fechamento explícito de épicos a partir do marcador ✅
        #    no master-briefing.md — fase 3.
        if tooling_path is not None:
            _enrich_epicos_closure(conn, tooling_path)

    return result


def _enrich_epicos_intencao(conn: sqlite3.Connection, tooling_root) -> None:
    """Preenche `epicos.intencao` para épicos cujo valor ainda é o slug placeholder.

    Não sobrescreve intenções já enriquecidas — leitura ESTRITAMENTE quando
    `intencao = id` (placeholder por construção do reconciler).
    """
    rows = conn.execute(
        "SELECT id, slug FROM epicos WHERE intencao = id"
    ).fetchall()
    for row in rows:
        intent = load_intent_text(tooling_root, row["slug"])
        if intent and intent != row["id"]:
            conn.execute(
                "UPDATE epicos SET intencao = ? WHERE id = ? AND intencao = ?",
                (intent, row["id"], row["id"]),
            )


def _enrich_epicos_closure(conn: sqlite3.Connection, tooling_root: Path) -> None:
    """Projeta fechamento explícito do briefing pra epicos.status/closed_at.

    Regra (source-of-truth.md §epicos.status):
      ✅ presente em intents/  + aberto                  → fecha (now)
      ✅ presente em archive/  + aberto                  → fecha (mtime do arquivo)
      ✅ presente              + fechado com closed_at   → no-op (preserva original)
      ✅ ausente em ambos      + fechado                 → reabre (status=aberto, closed_at=NULL)
      ✅ ausente em ambos      + aberto                  → no-op
      briefing ausente em ambos                          → tratado como ✅ ausente

    Archive é fallback secundário pra cobrir épicos cujos briefings foram
    arquivados pelo `mmb-reset.sh`. `closed_at` derivado de mtime é
    aproximação — fidelidade absoluta ao instante de fechamento original
    não é garantida. Campos humanos não são tocados.
    """
    rows = conn.execute(
        "SELECT id, slug, status, closed_at FROM epicos"
    ).fetchall()
    now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    for row in rows:
        text = load_briefing_text(tooling_root, row["slug"])
        closed_at_from_archive: str | None = None
        if text is None:
            text, closed_at_from_archive = load_archived_briefing(
                tooling_root, row["slug"]
            )

        is_closed_marker = bool(text) and parse_closed_marker(text)

        if is_closed_marker:
            if row["status"] != "fechado" or row["closed_at"] is None:
                conn.execute(
                    "UPDATE epicos SET status='fechado', closed_at=? WHERE id=?",
                    (closed_at_from_archive or now, row["id"]),
                )
        else:
            if row["status"] == "fechado":
                conn.execute(
                    "UPDATE epicos SET status='aberto', closed_at=NULL WHERE id=?",
                    (row["id"],),
                )


def _reset_derived_state(conn: sqlite3.Connection) -> None:
    """Apaga ciclos + épicos. Eventos órfãos vão em CASCADE.

    Não toca projetos, processed_files, jsonl_cursor.
    """
    n_ciclos = conn.execute("SELECT COUNT(*) AS n FROM ciclos").fetchone()["n"]
    n_epicos = conn.execute("SELECT COUNT(*) AS n FROM epicos").fetchone()["n"]
    conn.execute("DELETE FROM ciclos")
    conn.execute("DELETE FROM epicos")
    print(
        f"[reconcile:reset] removidos {n_ciclos} ciclos + {n_epicos} épicos "
        f"(eventos cascateados)",
        file=sys.stderr,
    )


def _process_issue(
    conn: sqlite3.Connection,
    issue: GhIssue,
    pr: GhPr | None,
    briefings_by_key: dict[str, Briefing],
    matched_briefing_paths: set[str],
    project_short: str,
    andaime_version: str | None,
    result: ReconcileResult,
    *,
    mmb_root: Path | None,
    claude_projects_root: Path | None,
    planner_models: dict[tuple[str, str], str] | None = None,
) -> None:
    """Projeta uma issue (+ PR opcional + briefing casado opcional)."""
    if "task" not in issue.labels:
        return

    epic_slug = epic_from_labels(issue.labels)
    if not epic_slug:
        result.warn(
            f"issue-without-epic-label: {issue.repo}#{issue.number} sem label epic:<slug>"
        )
        return

    anchor = parse_anchor(issue.body)
    matched_briefing: Briefing | None = None

    if anchor is None:
        result.warn(
            f"missing-anchor: {issue.repo}#{issue.number} sem mmb-cycle-key "
            f"no body (usando issue.createdAt como fallback)"
        )
        anchor_ts: str | None = None
    else:
        if anchor.epic_slug != epic_slug:
            result.warn(
                f"anchor-mismatch: {issue.repo}#{issue.number} âncora "
                f"epic={anchor.epic_slug} difere de label epic:{epic_slug}"
            )
        if anchor.project_short != project_short:
            result.warn(
                f"anchor-mismatch: {issue.repo}#{issue.number} âncora "
                f"project={anchor.project_short} difere de repo={project_short}"
            )
        anchor_ts = anchor.briefing_ts
        # Tenta casar com briefing real
        matched_briefing = briefings_by_key.get(anchor.cycle_key)
        if matched_briefing is None:
            result.warn(
                f"orphan-issue: {issue.repo}#{issue.number} âncora "
                f"{anchor.cycle_key} não casa com briefing em inbox"
            )
        else:
            matched_briefing_paths.add(matched_briefing.path)

    cycle_id = make_cycle_id(epic_slug, project_short, anchor_ts, issue.created_at)

    # Epico: started_at = MIN(briefing.created se houver, issue.created_at)
    epic_started_candidates = [issue.created_at]
    if matched_briefing is not None:
        epic_started_candidates.append(matched_briefing.created)
    epic_started = min(epic_started_candidates)

    inserted = _upsert_epico_min(conn, epic_slug, epic_started, andaime_version)
    if inserted:
        result.epicos_upserted += 1

    status = derive_status_with_briefing(matched_briefing, issue, pr, None)
    planner_invoked_at = (
        matched_briefing.created if matched_briefing else (anchor_ts or issue.created_at)
    )
    instruction = (
        matched_briefing.subject if matched_briefing else (issue.title or "(sem título)")
    )

    # Fase 4: cost + tokens via transcript se houver PR. Resultado None →
    # campos voltam pra NULL (caso transcript desapareça entre runs).
    cost_result: CostResult | None = None
    if (
        pr is not None
        and mmb_root is not None
        and claude_projects_root is not None
    ):
        cost_result = compute_cost_for_ciclo(
            mmb_root=mmb_root,
            repo=issue.repo,
            head_ref_name=pr.head_ref_name,
            claude_projects_root=claude_projects_root,
            warn=result.warn,
            ciclo_id=cycle_id,
        )

    model = (planner_models or {}).get((epic_slug, project_short)) or (
        cost_result.model if cost_result is not None else None
    )
    if status == "completo" and model is None:
        if cost_result is None:
            reason = "sem transcript"
        else:
            reason = "transcript sem modelo identificado"
        result.warn(
            f"model-not-derivable: cycle_id={cycle_id} ({reason})"
        )
    derived = _build_derived(
        status=status,
        pr=pr,
        briefing=matched_briefing,
        abort_signal=None,
        andaime_version=andaime_version,
        cost_result=cost_result,
        model=model,
    )

    _upsert_ciclo_selective(
        conn,
        cycle_id=cycle_id,
        epico_id=epic_slug,
        repo=issue.repo,
        planner_invoked_at=planner_invoked_at,
        instruction=instruction,
        derived=derived,
    )
    result.ciclos_upserted += 1


def _process_unmatched_briefings(
    conn: sqlite3.Connection,
    briefings: list[Briefing],
    journal_signals: list,
    agent_signals: list,
    now_epoch: float,
    stale_threshold_s: int,
    andaime_version: str | None,
    result: ReconcileResult,
    *,
    repo: str,
    planner_models: dict[tuple[str, str], str] | None = None,
) -> None:
    """Briefings sem issue casada — criam ciclo iniciado ou abortado pré-GH.

    Também detecta caso de múltiplos briefings concorrentes sem issue pra mesmo
    (epic, project) que terminam em estado `iniciado` (não terminal).
    """
    # status final por briefing pra detectar ambiguidade depois
    status_by_briefing: dict[str, str] = {}

    for b in briefings:
        signal = detect_abort_signal(
            briefing_created=b.created,
            project_short=b.project_short,
            journal_signals=journal_signals,
            agent_signals=agent_signals,
            now_epoch=now_epoch,
            stale_threshold_s=stale_threshold_s,
        )
        status = "abortado" if signal else "iniciado"
        status_by_briefing[b.path] = status

        # Epico
        inserted = _upsert_epico_min(conn, b.epic_slug, b.created, andaime_version)
        if inserted:
            result.epicos_upserted += 1

        # Briefing-only: sem PR, logo sem cost_result. Forma simétrica
        # com _process_issue: fallback explícito ainda que cost_result=None.
        cost_result: CostResult | None = None
        model = (planner_models or {}).get((b.epic_slug, b.project_short)) or (
            cost_result.model if cost_result is not None else None
        )
        derived = _build_derived(
            status=status,
            pr=None,
            briefing=b,
            abort_signal=signal,
            andaime_version=andaime_version,
            model=model,
        )

        _upsert_ciclo_selective(
            conn,
            cycle_id=b.cycle_id,
            epico_id=b.epic_slug,
            repo=repo,
            planner_invoked_at=b.created,
            instruction=b.subject,
            derived=derived,
        )
        result.ciclos_upserted += 1

    # Ambiguidade: 2+ briefings em `iniciado` pra mesmo (epic, project).
    by_pair: dict[tuple[str, str], list[Briefing]] = defaultdict(list)
    for b in briefings:
        if status_by_briefing.get(b.path) == "iniciado":
            by_pair[(b.epic_slug, b.project_short)].append(b)
    for (epic, project), blist in by_pair.items():
        if len(blist) <= 1:
            continue
        result.warn(
            f"multiple-briefings-no-issue: {len(blist)} briefings 'iniciado' pra "
            f"(epic={epic}, project={project}) sem issue casada: "
            + ", ".join(b.created for b in sorted(blist, key=lambda x: x.created))
        )


def _build_derived(
    *,
    status: str,
    pr: GhPr | None,
    briefing: Briefing | None,
    abort_signal: AbortSignal | None,
    andaime_version: str | None,
    cost_result: CostResult | None = None,
    model: str | None = None,
) -> dict[str, object]:
    """Constrói o dict de colunas derivadas pro UPSERT.

    Todas as chaves de DERIVED_COLS precisam estar presentes — quando
    inaplicáveis, valor é None. UPSERT reescreve essas colunas a cada run.
    """
    if pr is not None:
        merged_flag = 1 if pr.merged_at else 0
        pr_url: str | None = pr.url
        pr_number: int | None = pr.number
        closed_partial_at: str | None = pr.created_at
        closed_complete_at: str | None = pr.merged_at
        diff_added: int | None = pr.additions
        diff_deleted: int | None = pr.deletions
        diff_files: int | None = pr.changed_files
    else:
        merged_flag = None
        pr_url = None
        pr_number = None
        closed_partial_at = None
        closed_complete_at = None
        diff_added = None
        diff_deleted = None
        diff_files = None

    if abort_signal is not None:
        abort_at: str | None = abort_signal.abort_at
        abort_origin: str | None = abort_signal.abort_origin
        abort_reason: str | None = abort_signal.abort_reason
    else:
        abort_at = None
        abort_origin = None
        abort_reason = None

    briefing_md: str | None = briefing.body if briefing is not None else None

    # Fase 4: cost + tokens. None → NULL no DB (transcript ausente / model
    # desconhecido / sem PR). UPSERT idempotente — se transcript desaparecer,
    # próxima reconcile devolve NULL e UPSERT escreve NULL (sem lixo antigo).
    if cost_result is not None:
        tokens_input: int | None = cost_result.tokens_input
        tokens_output: int | None = cost_result.tokens_output
        cost_usd: float | None = cost_result.cost_usd
    else:
        tokens_input = None
        tokens_output = None
        cost_usd = None

    return {
        "status": status,
        "pr_url": pr_url,
        "pr_number": pr_number,
        "merged_to_main": merged_flag,
        "closed_partial_at": closed_partial_at,
        "closed_complete_at": closed_complete_at,
        "diff_added": diff_added,
        "diff_deleted": diff_deleted,
        "diff_files": diff_files,
        "andaime_version": andaime_version,
        "briefing_md": briefing_md,
        "abort_at": abort_at,
        "abort_origin": abort_origin,
        "abort_reason": abort_reason,
        "cost_usd": cost_usd,
        "tokens_input": tokens_input,
        "tokens_output": tokens_output,
        "model": model,
    }


def _upsert_epico_min(
    conn: sqlite3.Connection,
    slug: str,
    earliest_ts: str,
    andaime_version: str | None,
) -> bool:
    """INSERT épico se ausente; UPDATE started_at se ts mais antigo aparecer.

    `intencao` fica como o próprio slug — placeholder até fase 3 ler
    master-briefing.md. Status inicializado como 'aberto'. Fechamento é
    projetado em _enrich_epicos_closure a partir do marcador ✅ no master-briefing.md.
    Retorna True se inseriu pela primeira vez.
    """
    existing = conn.execute(
        "SELECT started_at FROM epicos WHERE id = ?", (slug,)
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO epicos (id, slug, started_at, intencao, status, andaime_version)
            VALUES (?, ?, ?, ?, 'aberto', ?)
            """,
            (slug, slug, earliest_ts, slug, andaime_version),
        )
        return True

    if earliest_ts < existing["started_at"]:
        conn.execute(
            "UPDATE epicos SET started_at = ? WHERE id = ?",
            (earliest_ts, slug),
        )
    return False


def _upsert_ciclo_selective(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    epico_id: str,
    repo: str,
    planner_invoked_at: str,
    instruction: str,
    derived: dict[str, object],
) -> None:
    """UPSERT que escreve só colunas pivot + colunas derivadas.

    INSERT: pivot + derivadas; colunas humanas (assertiveness_score,
    review_note) ficam NULL.
    UPDATE: SÓ derivadas. Nunca toca campos humanos nem `cost_*`, `tokens_*`
    (fase 4).
    """
    # ciclos.project = target.repo (convenção PR #34 + L10): internos
    # mantêm prefixo `mmb-` por conta do nome do repo; externos como
    # `campo-premiado` ficam sem prefixo. Antes derivávamos de
    # `project_short` com `f"mmb-{...}"`, o que rotulava ciclos externos
    # como `mmb-campo-premiado` e desalinhava da tabela `projetos`.
    project_full = repo

    existing = conn.execute("SELECT 1 FROM ciclos WHERE id = ?", (cycle_id,)).fetchone()

    if existing is None:
        cols = ["id", "epico_id", "project", "planner_invoked_at", "instruction"]
        vals: list[object] = [
            cycle_id,
            epico_id,
            project_full,
            planner_invoked_at,
            instruction,
        ]
        for k in DERIVED_COLS:
            cols.append(k)
            vals.append(derived[k])
        placeholders = ",".join("?" for _ in cols)
        conn.execute(
            f"INSERT INTO ciclos ({','.join(cols)}) VALUES ({placeholders})",
            vals,
        )
    else:
        sets = [f"{k} = ?" for k in DERIVED_COLS]
        vals = [derived[k] for k in DERIVED_COLS]
        vals.append(cycle_id)
        conn.execute(
            f"UPDATE ciclos SET {', '.join(sets)} WHERE id = ?",
            vals,
        )
