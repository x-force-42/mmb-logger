"""Testes do backfill heurístico de `ciclos.model` (T3).

Cobertura:
- `parse_mmb_mode_windows`: ler git log de um repo de teste e extrair
  janelas por mode default.
- `find_window`: matching de timestamp em janelas adjacentes.
- `backfill_model` end-to-end com janelas injetadas (sem depender de
  git real):
  - Backfill correto: ciclo em janela X → modelo Y.
  - Idempotência: rodar 2x = mesmo resultado, sem duplicar warnings.
  - Warning ambíguo: ciclo fora de qualquer janela → NULL + journal.
  - Borda pré-MMB_MODE: abortado pré-janela é silenciado.
  - `--dry-run` (via flag direta) não muta DB nem journal.
  - Domínio derivado vs humano: `assertiveness_score` preservado.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mmb_logger.backfill.model import (
    PLANNER_MODEL_BY_MODE,
    WARNING_EVENT_AMBIGUOUS,
    ModeWindow,
    backfill_model,
    find_window,
    parse_mmb_mode_windows,
)
from mmb_logger.db import get_conn, patch_ciclo

# ── Helpers ────────────────────────────────────────────────────────────


def _insert_ciclo(
    conn,
    *,
    cycle_id: str,
    epic: str = "test-epic",
    project: str = "mmb-core",
    planner_invoked_at: str,
    status: str = "completo",
    closed_complete_at: str | None = "2026-05-15T12:00:00Z",
    model: str | None = None,
    assertiveness_score: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO epicos (id, slug, started_at, intencao, status)
        VALUES (?, ?, ?, ?, 'aberto')
        ON CONFLICT(id) DO NOTHING
        """,
        (epic, epic, planner_invoked_at, epic),
    )
    conn.execute(
        """
        INSERT INTO ciclos
          (id, epico_id, project, planner_invoked_at, status, instruction,
           closed_complete_at, model)
        VALUES (?, ?, ?, ?, ?, 't', ?, ?)
        """,
        (
            cycle_id,
            epic,
            project,
            planner_invoked_at,
            status,
            closed_complete_at,
            model,
        ),
    )
    if assertiveness_score is not None:
        patch_ciclo(conn, cycle_id, assertiveness_score=assertiveness_score)
    conn.commit()


def _make_windows() -> list[ModeWindow]:
    """Janelas fixtura: pre-mmb-mode → normal → fast → balanced (corrente)."""
    return [
        ModeWindow(
            start=datetime(2026, 5, 10, 0, 0, tzinfo=UTC),
            end=datetime(2026, 5, 14, 23, 0, tzinfo=UTC),
            mode="pre-mmb-mode",
        ),
        ModeWindow(
            start=datetime(2026, 5, 14, 23, 0, tzinfo=UTC),
            end=datetime(2026, 5, 16, 12, 0, tzinfo=UTC),
            mode="normal",
        ),
        ModeWindow(
            start=datetime(2026, 5, 16, 12, 0, tzinfo=UTC),
            end=datetime(2026, 5, 17, 12, 0, tzinfo=UTC),
            mode="fast",
        ),
        ModeWindow(
            start=datetime(2026, 5, 17, 12, 0, tzinfo=UTC),
            end=None,
            mode="balanced",
        ),
    ]


@pytest.fixture()
def journal_path(tmp_path: Path) -> Path:
    p = tmp_path / "tooling-fake" / "logs" / "journal.jsonl"
    p.parent.mkdir(parents=True)
    return p


# ── find_window ────────────────────────────────────────────────────────


def test_find_window_matches_correct_segment():
    windows = _make_windows()
    # No coração da janela normal
    w = find_window(windows, datetime(2026, 5, 15, 10, 0, tzinfo=UTC))
    assert w is not None
    assert w.mode == "normal"

    # No coração da janela fast
    w = find_window(windows, datetime(2026, 5, 16, 18, 0, tzinfo=UTC))
    assert w is not None and w.mode == "fast"

    # Na janela balanced (corrente)
    w = find_window(windows, datetime(2026, 6, 1, 0, 0, tzinfo=UTC))
    assert w is not None and w.mode == "balanced"


def test_find_window_boundaries_are_left_inclusive():
    windows = _make_windows()
    # Exatamente em normal.start → cai em normal.
    boundary = datetime(2026, 5, 14, 23, 0, tzinfo=UTC)
    w = find_window(windows, boundary)
    assert w is not None and w.mode == "normal"

    # Exatamente em fast.start → cai em fast (não em normal).
    boundary2 = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)
    w2 = find_window(windows, boundary2)
    assert w2 is not None and w2.mode == "fast"


def test_find_window_before_first_returns_none():
    windows = _make_windows()
    w = find_window(windows, datetime(2026, 5, 1, 0, 0, tzinfo=UTC))
    assert w is None


def test_planner_model_lookup_via_window_property():
    win = ModeWindow(
        start=datetime(2026, 5, 1, tzinfo=UTC), end=None, mode="normal"
    )
    assert win.planner_model == PLANNER_MODEL_BY_MODE["normal"]
    unknown = ModeWindow(
        start=datetime(2026, 5, 1, tzinfo=UTC), end=None, mode="pre-mmb-mode"
    )
    assert unknown.planner_model is None


# ── parse_mmb_mode_windows ─────────────────────────────────────────────


def test_parse_mmb_mode_windows_from_synthetic_repo(tmp_path: Path):
    """Cria um mini repo git com 3 versões de config.sh, espera 3 janelas."""
    repo = tmp_path / "mmb-fake"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)

    tooling = repo / ".tooling"
    tooling.mkdir()
    config = tooling / "config.sh"

    def commit(content: str, msg: str, date: str):
        config.write_text(content)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        env = os.environ.copy()
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
        subprocess.run(
            ["git", "commit", "-q", "-m", msg],
            cwd=repo,
            check=True,
            env=env,
        )

    # v1: sem MMB_MODE (pre-mmb-mode)
    commit(
        '#!/bin/bash\n: "${MMB_MODEL_MASTER:=claude-opus-4-7}"\n',
        "v1",
        "2026-05-10T00:00:00+0000",
    )
    # v2: introduz MMB_MODE=normal
    commit(
        '#!/bin/bash\n: "${MMB_MODE:=normal}"\n',
        "v2",
        "2026-05-14T23:00:00+0000",
    )
    # v3: muda default pra fast
    commit(
        '#!/bin/bash\n: "${MMB_MODE:=fast}"\n',
        "v3",
        "2026-05-16T12:00:00+0000",
    )

    windows = parse_mmb_mode_windows(repo)

    assert [w.mode for w in windows] == ["pre-mmb-mode", "normal", "fast"]
    assert windows[0].end == windows[1].start
    assert windows[1].end == windows[2].start
    assert windows[2].end is None
    # Sanidade dos timestamps
    assert windows[1].start == datetime(2026, 5, 14, 23, 0, tzinfo=UTC)


def test_parse_mmb_mode_windows_coalesces_consecutive_duplicates(
    tmp_path: Path,
):
    """Commits seguidos com mesmo mode não viram janelas separadas."""
    repo = tmp_path / "mmb-fake-coalesce"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    tooling = repo / ".tooling"
    tooling.mkdir()
    config = tooling / "config.sh"

    def commit(content: str, msg: str, date: str):
        config.write_text(content)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        env = os.environ.copy()
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
        subprocess.run(
            ["git", "commit", "-q", "-m", msg], cwd=repo, check=True, env=env
        )

    commit(': "${MMB_MODE:=normal}"\n', "v1", "2026-05-14T23:00:00+0000")
    commit(': "${MMB_MODE:=normal}"\n# noop\n', "v2", "2026-05-15T00:00:00+0000")
    commit(': "${MMB_MODE:=fast}"\n', "v3", "2026-05-16T00:00:00+0000")

    windows = parse_mmb_mode_windows(repo)
    assert [w.mode for w in windows] == ["normal", "fast"]


# ── backfill_model end-to-end ──────────────────────────────────────────


def test_backfill_populates_model_from_window(db_path: Path, journal_path: Path):
    windows = _make_windows()
    with get_conn(db_path) as conn:
        _insert_ciclo(
            conn,
            cycle_id="c-normal",
            planner_invoked_at="2026-05-15T10:00:00Z",
        )
        _insert_ciclo(
            conn,
            cycle_id="c-fast",
            planner_invoked_at="2026-05-16T18:00:00Z",
        )
        _insert_ciclo(
            conn,
            cycle_id="c-balanced",
            planner_invoked_at="2026-05-17T18:00:00Z",
        )

    result = backfill_model(
        db_path=db_path,
        journal_path=journal_path,
        windows=windows,
    )

    assert result.candidates == 3
    assert result.backfilled == 3
    assert result.ambiguous == 0
    assert result.by_model == {
        PLANNER_MODEL_BY_MODE["normal"]: 1,
        PLANNER_MODEL_BY_MODE["fast"]: 1,
        PLANNER_MODEL_BY_MODE["balanced"]: 1,
    }

    with get_conn(db_path) as conn:
        rows = {
            r["id"]: r["model"]
            for r in conn.execute("SELECT id, model FROM ciclos").fetchall()
        }
    assert rows["c-normal"] == PLANNER_MODEL_BY_MODE["normal"]
    assert rows["c-fast"] == PLANNER_MODEL_BY_MODE["fast"]
    assert rows["c-balanced"] == PLANNER_MODEL_BY_MODE["balanced"]


def test_backfill_is_idempotent(db_path: Path, journal_path: Path):
    windows = _make_windows()
    with get_conn(db_path) as conn:
        _insert_ciclo(
            conn,
            cycle_id="c1",
            planner_invoked_at="2026-05-15T10:00:00Z",
        )
        # Ciclo ambíguo (antes da pre-mmb-mode → no-window)
        _insert_ciclo(
            conn,
            cycle_id="c-orphan",
            planner_invoked_at="2026-05-01T10:00:00Z",
        )

    r1 = backfill_model(
        db_path=db_path, journal_path=journal_path, windows=windows
    )
    r2 = backfill_model(
        db_path=db_path, journal_path=journal_path, windows=windows
    )

    assert r1.backfilled == 1
    # Segunda run: c1 já tem model populado → não conta como candidato.
    assert r2.candidates == 1  # só c-orphan ainda NULL
    assert r2.backfilled == 0

    # Warning ambíguo emitido só uma vez (dedup via journal).
    assert r1.warnings_emitted == 1
    assert r2.warnings_emitted == 0
    assert r2.warnings_skipped_dedup == 1

    # Conta linhas backfill-model-ambiguous no journal.
    lines = [
        json.loads(line)
        for line in journal_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ambig = [
        entry
        for entry in lines
        if entry.get("event") == WARNING_EVENT_AMBIGUOUS
    ]
    assert len(ambig) == 1
    assert ambig[0]["payload"]["cycle_id"] == "c-orphan"


def test_backfill_ambiguous_no_window(db_path: Path, journal_path: Path):
    windows = _make_windows()
    with get_conn(db_path) as conn:
        _insert_ciclo(
            conn,
            cycle_id="c-pre",
            planner_invoked_at="2026-04-01T10:00:00Z",
            status="completo",
        )

    result = backfill_model(
        db_path=db_path, journal_path=journal_path, windows=windows
    )

    assert result.candidates == 1
    assert result.backfilled == 0
    assert result.ambiguous == 1
    assert result.warnings_emitted == 1

    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT model FROM ciclos WHERE id = ?", ("c-pre",)
        ).fetchone()
    assert row["model"] is None  # preservado NULL

    lines = [
        json.loads(line)
        for line in journal_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ambig = [entry for entry in lines if entry.get("event") == WARNING_EVENT_AMBIGUOUS]
    assert len(ambig) == 1
    assert ambig[0]["sev"] == "warn"
    assert ambig[0]["payload"]["cycle_id"] == "c-pre"
    assert ambig[0]["payload"]["reason"] == "no-window-match"


def test_backfill_skips_pre_window_aborted_silently(
    db_path: Path, journal_path: Path
):
    """Abortados anteriores à janela MMB_MODE=normal: NULL preservado, sem warn."""
    windows = _make_windows()
    with get_conn(db_path) as conn:
        _insert_ciclo(
            conn,
            cycle_id="c-old-abort",
            planner_invoked_at="2026-05-01T10:00:00Z",
            status="abortado",
        )

    result = backfill_model(
        db_path=db_path, journal_path=journal_path, windows=windows
    )

    assert result.skipped_pre_window_abort == 1
    assert result.ambiguous == 0
    assert result.warnings_emitted == 0
    assert not journal_path.exists() or not journal_path.read_text().strip()


def test_backfill_dry_run_does_not_mutate(db_path: Path, journal_path: Path):
    windows = _make_windows()
    with get_conn(db_path) as conn:
        _insert_ciclo(
            conn,
            cycle_id="c1",
            planner_invoked_at="2026-05-15T10:00:00Z",
        )
        _insert_ciclo(
            conn,
            cycle_id="c-orphan",
            planner_invoked_at="2026-05-01T10:00:00Z",
        )

    result = backfill_model(
        db_path=db_path,
        journal_path=journal_path,
        windows=windows,
        dry_run=True,
    )

    assert result.dry_run is True
    assert result.candidates == 2
    assert result.backfilled == 1  # contaria, mas não escreveu
    assert result.ambiguous == 1

    with get_conn(db_path) as conn:
        rows = {
            r["id"]: r["model"]
            for r in conn.execute("SELECT id, model FROM ciclos").fetchall()
        }
    assert rows["c1"] is None  # NÃO foi escrito
    assert rows["c-orphan"] is None

    # Journal: NÃO foi escrito mesmo pro ambíguo.
    assert not journal_path.exists() or not journal_path.read_text().strip()


def test_backfill_skips_already_populated(db_path: Path, journal_path: Path):
    """Ciclo com `model` já populado por T2 não vira candidato."""
    windows = _make_windows()
    with get_conn(db_path) as conn:
        _insert_ciclo(
            conn,
            cycle_id="c-already",
            planner_invoked_at="2026-05-15T10:00:00Z",
            model="claude-sonnet-4-6",  # populado por T2
        )

    result = backfill_model(
        db_path=db_path, journal_path=journal_path, windows=windows
    )

    assert result.candidates == 0
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT model FROM ciclos WHERE id = ?", ("c-already",)
        ).fetchone()
    # Modelo originalmente populado preservado.
    assert row["model"] == "claude-sonnet-4-6"


def test_backfill_skips_open_ciclos(db_path: Path, journal_path: Path):
    """Ciclos em-voo (closed_complete_at NULL) não são tocados."""
    windows = _make_windows()
    with get_conn(db_path) as conn:
        _insert_ciclo(
            conn,
            cycle_id="c-open",
            planner_invoked_at="2026-05-15T10:00:00Z",
            status="iniciado",
            closed_complete_at=None,
        )

    result = backfill_model(
        db_path=db_path, journal_path=journal_path, windows=windows
    )
    assert result.candidates == 0
    assert result.backfilled == 0


def test_backfill_preserves_humans(db_path: Path, journal_path: Path):
    """assertiveness_score e review_note ficam intocados."""
    windows = _make_windows()
    with get_conn(db_path) as conn:
        _insert_ciclo(
            conn,
            cycle_id="c-h",
            planner_invoked_at="2026-05-15T10:00:00Z",
            assertiveness_score=5,
        )
        patch_ciclo(conn, "c-h", review_note="nota humana")
        conn.commit()

    backfill_model(
        db_path=db_path, journal_path=journal_path, windows=windows
    )

    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT model, assertiveness_score, review_note FROM ciclos "
            "WHERE id = ?",
            ("c-h",),
        ).fetchone()
    assert row["model"] == PLANNER_MODEL_BY_MODE["normal"]
    assert row["assertiveness_score"] == 5
    assert row["review_note"] == "nota humana"
