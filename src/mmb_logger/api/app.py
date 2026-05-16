"""FastAPI app factory + middleware + injeção do path do DB.

Use `create_app(db_path=...)` em testes pra apontar pra DB temporário.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from mmb_logger import __version__
from mmb_logger.api.routes import ciclos, epicos, eventos, metricas, projetos
from mmb_logger.db import resolve_db_path


def create_app(db_path: str | os.PathLike[str] | None = None) -> FastAPI:
    """Cria FastAPI app. `db_path` sobrescreve resolução padrão se fornecido."""
    resolved = resolve_db_path(db_path) if db_path else resolve_db_path()
    app = FastAPI(
        title="mmb-logger",
        version=__version__,
        description="API de leitura/escrita pro Cockpit consumir épicos e ciclos do andaime.",
    )

    app.state.db_path = Path(resolved)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_methods=["GET", "PATCH", "OPTIONS"],
        allow_headers=["*"],
        allow_credentials=False,
    )

    app.include_router(epicos.router)
    app.include_router(ciclos.router)
    app.include_router(eventos.router)
    app.include_router(projetos.router)
    app.include_router(metricas.router)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


# Instância default pro uvicorn pegar como `mmb_logger.api.app:app`.
app = create_app()
