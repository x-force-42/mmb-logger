"""CLI do mmb-logger (typer)."""

from __future__ import annotations

from pathlib import Path

import typer

from mmb_logger import __version__
from mmb_logger.db import init_db as _init_db
from mmb_logger.db import resolve_db_path

app = typer.Typer(
    add_completion=False,
    help="mmb-logger — sistema de logs do andaime MMB.",
    no_args_is_help=True,
)


@app.command("version")
def version_cmd() -> None:
    """Imprime versão do pacote."""
    typer.echo(__version__)


@app.command("init-db")
def init_db_cmd(
    db: str | None = typer.Option(
        None, "--db", help="Caminho do SQLite (default: ./mmb-logger.db ou env)."
    ),
) -> None:
    """Cria/atualiza o schema do banco. Idempotente."""
    path = _init_db(db_path=db)
    typer.echo(f"DB pronto em: {path}")


@app.command("serve")
def serve_cmd(
    db: str | None = typer.Option(None, "--db", help="Caminho do SQLite."),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8765, "--port"),
    reload: bool = typer.Option(False, "--reload", help="Reload no dev (não usa em prod)."),
) -> None:
    """Sobe a API HTTP (FastAPI + uvicorn)."""
    import os

    if db:
        os.environ["MMB_LOGGER_DB_PATH"] = str(Path(db).resolve())
    resolved = resolve_db_path()
    if not Path(resolved).is_file():
        typer.secho(
            f"Aviso: DB {resolved} não existe. Rode `mmb-logger init-db` antes.",
            fg=typer.colors.YELLOW,
            err=True,
        )

    import uvicorn

    uvicorn.run(
        "mmb_logger.api.app:app",
        host=host,
        port=port,
        reload=reload,
    )


@app.command("reconcile")
def reconcile_cmd(
    db: str | None = typer.Option(None, "--db", help="Caminho do SQLite."),
    reset: bool = typer.Option(
        False,
        "--reset",
        help=(
            "DESTRUTIVO: apaga ciclos + epicos antes (eventos vão em CASCADE, "
            "incluindo assertiveness_score e review_note dos ciclos atuais). "
            "Use somente em cutover/rebuild controlado com snapshot prévio. "
            "Operação normal é reconcile aditivo (sem --reset)."
        ),
    ),
    owner: str = typer.Option("x-force-42", "--owner", help="GH owner/org."),
) -> None:
    """Reconcile (fases 1-4).

    Projeta o estado canônico do método em ciclos/épicos/eventos:
    - Fase 1: GitHub issues + PRs → planejado / pr_aberto / completo / abortado-pós-GH.
    - Fase 2: dispatch master→planner em inbox/ → iniciado, aborto pré-GH.
    - Fase 3: audit eventos de journal/agents/inbox (sem transicionar estado);
      epicos.intencao do master-briefing.md.

    Preserva colunas humanas (assertiveness_score, review_note) via UPSERT
    seletivo. Eventos audit usam UNIQUE source_key — idempotente.

    Contrato em /MMB/.tooling/source-of-truth.md.
    """
    from mmb_logger.reconcile.reconcile import reconcile as _reconcile

    if reset:
        typer.secho(
            "⚠  --reset é DESTRUTIVO:",
            fg=typer.colors.RED,
            bold=True,
            err=True,
        )
        typer.secho(
            "   - DELETE FROM ciclos + epicos antes de reconciliar.\n"
            "   - eventos órfãos vão em CASCADE.\n"
            "   - assertiveness_score e review_note dos ciclos atuais SERÃO PERDIDOS\n"
            "     (cascateados junto com a row do ciclo).\n"
            "   - projetos, processed_files e jsonl_cursor são preservados.\n"
            "\n"
            "   Use somente em cutover/rebuild controlado. Tenha um snapshot\n"
            "   do DB antes (cp mmb-logger.db mmb-logger.pre-reset.sqlite).",
            fg=typer.colors.YELLOW,
            err=True,
        )

    try:
        result = _reconcile(db_path=db, owner=owner, reset=reset)
    except RuntimeError as exc:
        typer.secho(f"Erro: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    typer.echo(f"épicos upserted: {result.epicos_upserted}")
    typer.echo(f"ciclos upserted: {result.ciclos_upserted}")
    typer.echo(f"warnings: {len(result.warnings)}")
    if result.warnings:
        typer.echo("Resumo dos primeiros 10 warnings (todos foram pro stderr):")
        for w in result.warnings[:10]:
            typer.echo(f"  - {w}")


@app.command("backfill-model")
def backfill_model_cmd(
    db: str | None = typer.Option(None, "--db", help="Caminho do SQLite."),
    tooling_root: str | None = typer.Option(
        None,
        "--tooling-root",
        help="Raiz do .tooling/ (default: env MMB_LOGGER_TOOLING_PATH).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Não escreve no DB nem no journal; só imprime contagens.",
    ),
) -> None:
    """Backfill heurístico de `ciclos.model` pra ciclos pré-T1.

    One-shot. Idempotente (re-rodar não regride; warnings deduplicados via
    journal). Mapeia janelas temporais do default de `MMB_MODE` em
    `.tooling/config.sh` pro modelo do planner default daquele modo.

    Só toca `ciclos` com `model IS NULL AND closed_complete_at IS NOT NULL`.
    Coluna humana (`assertiveness_score`, `review_note`) é intocada por
    construção — UPDATE só escreve `model`. Ciclos `abortado` anteriores
    à primeira janela MMB_MODE=normal são silenciados (borda histórica).
    """
    from mmb_logger.backfill.model import backfill_model as _backfill

    try:
        result = _backfill(
            db_path=db,
            tooling_root=tooling_root,
            dry_run=dry_run,
        )
    except RuntimeError as exc:
        typer.secho(f"Erro: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    prefix = "[dry-run] " if dry_run else ""
    typer.echo(f"{prefix}candidatos (model NULL + closed_complete): {result.candidates}")
    typer.echo(f"{prefix}backfilled: {result.backfilled}")
    if result.by_model:
        for model, n in sorted(result.by_model.items()):
            typer.echo(f"  {model}: {n}")
    typer.echo(f"{prefix}ambíguos (NULL preservado): {result.ambiguous}")
    typer.echo(f"  warnings emitidos no journal: {result.warnings_emitted}")
    typer.echo(f"  warnings deduplicados (já no journal): {result.warnings_skipped_dedup}")
    typer.echo(f"{prefix}abortados pré-MMB_MODE pulados: {result.skipped_pre_window_abort}")
    if result.ambiguous_samples:
        typer.echo("Sample de ambíguos (primeiros 10):")
        for s in result.ambiguous_samples:
            typer.echo(
                f"  - {s['cycle_id']} ({s['planner_invoked_at']}) → {s['reason']}"
            )


@app.command("backfill-agent-sessions")
def backfill_agent_sessions_cmd(
    db: str | None = typer.Option(None, "--db", help="Caminho do SQLite."),
    mmb_root: str | None = typer.Option(
        None, "--mmb-root", help="Raiz do MMB (default: walk-up procurando .tooling/)."
    ),
    claude_projects: str | None = typer.Option(
        None,
        "--claude-projects",
        help="Diretório de transcripts Claude (default: ~/.claude/projects).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Não escreve no DB; só agrega e reporta."
    ),
    write: bool = typer.Option(
        False, "--write", help="Persiste no DB. Mutuamente exclusivo com --dry-run."
    ),
    no_gh: bool = typer.Option(
        False,
        "--no-gh",
        help="Pula chamadas gh (offline). Atomics ficam LOW sem cross-GH.",
    ),
    limit: int | None = typer.Option(
        None, "--limit", help="Processa só N sessões (debug)."
    ),
    output_jsonl: str | None = typer.Option(
        None,
        "--output-jsonl",
        help="Exporta dataset pra JSONL (independente de --dry-run/--write).",
    ),
) -> None:
    """Backfill retroativo de sessões Claude Code → `agent_sessions`.

    Lê transcripts em ~/.claude/projects, classifica role, normaliza task_id,
    cruza com GitHub pra promover linkagem, e popula a tabela `agent_sessions`
    via UPSERT idempotente por `session_id`.

    Custo é estimativa (cost_usd_estimated + cost_pricing_version +
    cost_confidence); NÃO substitui ciclos.cost_usd. ORPHAN é estado válido
    ("sem ciclo MMB único confiável"), persistido com ciclo_id=NULL.

    Fail-safe: sem --dry-run nem --write, o comando recusa rodar.
    """
    from mmb_logger.backfill.agent_sessions import (
        backfill_agent_sessions as _backfill,
    )

    try:
        result = _backfill(
            db_path=db,
            mmb_root=mmb_root,
            claude_projects=claude_projects,
            write=write,
            dry_run=dry_run,
            no_gh=no_gh,
            limit=limit,
            output_jsonl=output_jsonl,
        )
    except ValueError as exc:
        typer.secho(f"Erro: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    except FileNotFoundError as exc:
        typer.secho(f"Erro: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    prefix = "[dry-run] " if result.dry_run else ""
    typer.echo(f"{prefix}sessões processadas: {result.sessions_processed}")
    if write:
        typer.echo(f"inseridas: {result.inserted}")
        typer.echo(f"atualizadas: {result.updated}")
    typer.echo(f"custo estimado total: US$ {result.cost_total_usd:.2f}")
    typer.echo("por role:")
    for role, n in sorted(result.by_role.items(), key=lambda kv: -kv[1]):
        cost = result.cost_by_role.get(role, 0.0)
        typer.echo(f"  {role:15} n={n:<5} cost_est=US$ {cost:.2f}")
    typer.echo("por link_confidence:")
    for conf in ("HIGH", "MEDIUM", "LOW", "ORPHAN"):
        n = result.by_confidence.get(conf, 0)
        cost = result.cost_by_confidence.get(conf, 0.0)
        typer.echo(f"  {conf:8} n={n:<5} cost_est=US$ {cost:.2f}")
    if result.top_by_cost:
        typer.echo("top 10 por custo:")
        for row in result.top_by_cost:
            typer.echo(
                f"  {row['session_id']} {row['role']:10} "
                f"{row['project'] or '-':10} {row.get('slug') or '-':40} "
                f"US$ {row['cost_usd_estimated']}"
            )
    if result.top_by_duration:
        typer.echo("top 10 por duração wall (min):")
        for row in result.top_by_duration:
            mins = row["duration_wall_ms"] / 60000
            typer.echo(
                f"  {row['session_id']} {row['role']:10} "
                f"{row['project'] or '-':10} {row.get('slug') or '-':40} "
                f"{mins:.1f} min"
            )
    if result.top_by_tool_calls:
        typer.echo("top 10 por tool calls:")
        for row in result.top_by_tool_calls:
            typer.echo(
                f"  {row['session_id']} {row['role']:10} "
                f"{row['project'] or '-':10} {row.get('slug') or '-':40} "
                f"{row['tool_call_count_total']} calls"
            )


