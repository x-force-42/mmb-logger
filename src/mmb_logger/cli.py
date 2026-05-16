"""CLI do mmb-logger (typer)."""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from mmb_logger import __version__
from mmb_logger.db import init_db as _init_db
from mmb_logger.db import resolve_db_path
from mmb_logger.ingest.runner import (
    ingest_once as _ingest_once,
)
from mmb_logger.ingest.runner import (
    resolve_tooling_root,
)
from mmb_logger.ingest.runner import (
    watch as _watch,
)

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


@app.command("ingest-once")
def ingest_once_cmd(
    db: str | None = typer.Option(None, "--db"),
    tooling: str | None = typer.Option(None, "--tooling", help="Raiz do .tooling/."),
) -> None:
    """Varredura única dos 3 fluxos. Idempotente."""
    root = resolve_tooling_root(tooling)
    if not Path(root).is_dir():
        typer.secho(f"Erro: tooling_root inválido: {root}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    counts = _ingest_once(db_path=db, tooling_root=root)
    typer.echo("Ingestão concluída:")
    for k, v in counts.items():
        typer.echo(f"  {k}: {v}")


@app.command("watch")
def watch_cmd(
    db: str | None = typer.Option(None, "--db"),
    tooling: str | None = typer.Option(None, "--tooling"),
) -> None:
    """Sobe watcher contínuo (catch-up + observer). Ctrl+C desliga."""
    root = resolve_tooling_root(tooling)
    if not Path(root).is_dir():
        typer.secho(f"Erro: tooling_root inválido: {root}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    try:
        _watch(db_path=db, tooling_root=root)
    except KeyboardInterrupt:  # pragma: no cover - sinal de usuário
        typer.echo("\nDesligado.", err=True)
        sys.exit(0)
