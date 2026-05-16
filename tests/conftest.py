"""Fixtures compartilhadas: DB temporário, app FastAPI, helpers de inserção."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mmb_logger.api.app import create_app
from mmb_logger.db import get_conn, init_db

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    os.environ.pop("MMB_LOGGER_DB_PATH", None)
    init_db(db_path=path)
    return path


@pytest.fixture()
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    with get_conn(db_path) as c:
        yield c


@pytest.fixture()
def client(db_path: Path) -> Iterator[TestClient]:
    app = create_app(db_path=db_path)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def tooling_root(tmp_path: Path) -> Path:
    """Cria árvore .tooling/ falsa pra testes de ingestão."""
    root = tmp_path / "tooling"
    (root / "inbox" / "master").mkdir(parents=True)
    (root / "inbox" / "core").mkdir(parents=True)
    (root / "inbox" / "cockpit").mkdir(parents=True)
    (root / "inbox" / "aquarium").mkdir(parents=True)
    (root / "logs").mkdir()
    (root / "state").mkdir()
    return root
