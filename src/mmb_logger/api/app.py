"""FastAPI app factory + middleware + injeção do path do DB.

Use `create_app(db_path=...)` em testes pra apontar pra DB temporário.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from mmb_logger import __version__
from mmb_logger.api.routes import andaime_versions, ciclos, epicos, eventos, metricas, projetos
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

    # CORS: defaults cobrem Vite dev (5173) E preview (4173) em ambos
    # localhost e 127.0.0.1. Override via env MMB_LOGGER_CORS_ORIGINS
    # (comma-separated) quando o cockpit estiver em outra origin.
    cors_env = os.environ.get("MMB_LOGGER_CORS_ORIGINS")
    if cors_env:
        cors_origins = [o.strip() for o in cors_env.split(",") if o.strip()]
    else:
        cors_origins = [
            "http://localhost:4173",   # vite preview (production build)
            "http://127.0.0.1:4173",
            "http://localhost:5173",   # vite dev (HMR)
            "http://127.0.0.1:5173",
        ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["GET", "PATCH", "OPTIONS"],
        allow_headers=["*"],
        allow_credentials=False,
    )

    app.include_router(epicos.router)
    app.include_router(ciclos.router)
    app.include_router(eventos.router)
    app.include_router(projetos.router)
    app.include_router(metricas.router)
    app.include_router(andaime_versions.router)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


# Instância default pro uvicorn pegar como `mmb_logger.api.app:app`.
app = create_app()
