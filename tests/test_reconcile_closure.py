"""Testes da fase 3 — fechamento explícito de épicos via marcador ✅.

Verifica:
- parse_closed_marker detecta (ou não) o marcador corretamente.
- _enrich_epicos_closure transiciona status/closed_at conforme regras.
- Idempotência: 2ª execução não altera closed_at já gravado.
- Reabertura: remover ✅ do briefing reabre o épico.
- Campos humanos (assertiveness_score, review_note em ciclos) preservados.
- Regressão: epicos.intencao continua sendo preenchido normalmente.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from mmb_logger.db import get_conn, patch_ciclo
from mmb_logger.reconcile.intents import load_archived_briefing, parse_closed_marker
from mmb_logger.reconcile.reconcile import _enrich_epicos_closure, reconcile

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def tooling(tmp_path: Path) -> Path:
    root = tmp_path / "tooling"
    (root / "intents").mkdir(parents=True)
    return root


def _insert_epico(
    conn,
    *,
    slug: str,
    status: str = "aberto",
    closed_at: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO epicos (id, slug, started_at, intencao, status, closed_at)
        VALUES (?, ?, '2026-05-16T10:00:00Z', ?, ?, ?)
        """,
        (slug, slug, slug, status, closed_at),
    )


def _write_briefing(tooling: Path, slug: str, content: str) -> None:
    dir_ = tooling / "intents" / f"2026-05-16-{slug}"
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / "master-briefing.md").write_text(content, encoding="utf-8")


def _write_archived_briefing(
    tooling: Path,
    slug: str,
    content: str,
    *,
    run_id: str = "2026-05-16T17-49-09Z",
    date_prefix: str = "2026-05-16",
    mtime: float | None = None,
) -> Path:
    dir_ = tooling / "archive" / run_id / "intents" / f"{date_prefix}-{slug}"
    dir_.mkdir(parents=True, exist_ok=True)
    path = dir_ / "master-briefing.md"
    path.write_text(content, encoding="utf-8")
    if mtime is not None:
        import os
        os.utime(path, (mtime, mtime))
    return path


# ── parse_closed_marker ───────────────────────────────────────────────────────


def test_parse_closed_marker_sem_status() -> None:
    assert parse_closed_marker("# Épico sem status\n\n## Detalhe\n") is False


def test_parse_closed_marker_com_checkmark() -> None:
    assert parse_closed_marker("# Épico\n\nStatus: ✅ fechado\n") is True


def test_parse_closed_marker_com_prefixo_lista() -> None:
    assert parse_closed_marker("- Status: ✅\n") is True
    assert parse_closed_marker("* Status: ✅ concluído\n") is True


def test_parse_closed_marker_outros_emoji_nao_fecham() -> None:
    assert parse_closed_marker("Status: ⏳ em execução\n") is False
    assert parse_closed_marker("Status: 🎯 ativo\n") is False
    assert parse_closed_marker("Status: ❌ abortado\n") is False


# ── _enrich_epicos_closure — casos de transição ───────────────────────────────


def test_sem_checkmark_epico_permanece_aberto(db_path: Path, tooling: Path) -> None:
    """Briefing sem ✅ → row aberto, closed_at IS NULL."""
    _write_briefing(tooling, "meu-epico", "# Épico em execução\n\nStatus: 🎯\n")
    with get_conn(db_path) as conn:
        _insert_epico(conn, slug="meu-epico")
        _enrich_epicos_closure(conn, tooling)
        row = conn.execute(
            "SELECT status, closed_at FROM epicos WHERE id='meu-epico'"
        ).fetchone()
    assert row["status"] == "aberto"
    assert row["closed_at"] is None


def test_com_checkmark_epico_fecha(db_path: Path, tooling: Path) -> None:
    """Briefing com ✅ → row fecha, closed_at NOT NULL."""
    _write_briefing(tooling, "meu-epico", "# Épico\n\nStatus: ✅ fechado\n")
    with get_conn(db_path) as conn:
        _insert_epico(conn, slug="meu-epico")
        _enrich_epicos_closure(conn, tooling)
        row = conn.execute(
            "SELECT status, closed_at FROM epicos WHERE id='meu-epico'"
        ).fetchone()
    assert row["status"] == "fechado"
    assert row["closed_at"] is not None


def test_idempotencia_closed_at_preservado(db_path: Path, tooling: Path) -> None:
    """Reconcile 2x consecutivas não altera closed_at original."""
    _write_briefing(tooling, "meu-epico", "# Épico\n\nStatus: ✅ fechado\n")
    with get_conn(db_path) as conn:
        _insert_epico(conn, slug="meu-epico")
        _enrich_epicos_closure(conn, tooling)
        first_closed_at = conn.execute(
            "SELECT closed_at FROM epicos WHERE id='meu-epico'"
        ).fetchone()["closed_at"]

        _enrich_epicos_closure(conn, tooling)
        second_closed_at = conn.execute(
            "SELECT closed_at FROM epicos WHERE id='meu-epico'"
        ).fetchone()["closed_at"]

    assert first_closed_at == second_closed_at


def test_remover_checkmark_reabre_epico(db_path: Path, tooling: Path) -> None:
    """Após remover ✅ do briefing, reconcile reabre (status=aberto, closed_at=NULL)."""
    _write_briefing(tooling, "meu-epico", "# Épico\n\nStatus: ✅ fechado\n")
    with get_conn(db_path) as conn:
        _insert_epico(conn, slug="meu-epico")
        _enrich_epicos_closure(conn, tooling)
        # Verifica fechado
        assert conn.execute(
            "SELECT status FROM epicos WHERE id='meu-epico'"
        ).fetchone()["status"] == "fechado"

    # Reescreve briefing sem ✅
    _write_briefing(tooling, "meu-epico", "# Épico\n\nStatus: 🎯 ativo\n")
    with get_conn(db_path) as conn:
        _enrich_epicos_closure(conn, tooling)
        row = conn.execute(
            "SELECT status, closed_at FROM epicos WHERE id='meu-epico'"
        ).fetchone()
    assert row["status"] == "aberto"
    assert row["closed_at"] is None


def test_epico_sem_briefing_permanece_aberto(db_path: Path, tooling: Path) -> None:
    """Épico sem briefing local → permanece aberto (não-erro)."""
    # Não cria nenhum briefing
    with get_conn(db_path) as conn:
        _insert_epico(conn, slug="sem-briefing")
        _enrich_epicos_closure(conn, tooling)
        row = conn.execute(
            "SELECT status, closed_at FROM epicos WHERE id='sem-briefing'"
        ).fetchone()
    assert row["status"] == "aberto"
    assert row["closed_at"] is None


def test_status_outro_emoji_nao_fecha(db_path: Path, tooling: Path) -> None:
    """'Status: ⏳ em execução' não fecha — regex não casa."""
    _write_briefing(tooling, "meu-epico", "# Épico\n\nStatus: ⏳ em execução\n")
    with get_conn(db_path) as conn:
        _insert_epico(conn, slug="meu-epico")
        _enrich_epicos_closure(conn, tooling)
        row = conn.execute(
            "SELECT status FROM epicos WHERE id='meu-epico'"
        ).fetchone()
    assert row["status"] == "aberto"


def test_campos_humanos_preservados(db_path: Path, tooling: Path) -> None:
    """assertiveness_score e review_note em ciclos não são tocados."""
    _write_briefing(tooling, "meu-epico", "# Épico\n\nStatus: ✅ fechado\n")
    with get_conn(db_path) as conn:
        _insert_epico(conn, slug="meu-epico")
        conn.execute(
            """
            INSERT INTO ciclos
              (id, epico_id, project, planner_invoked_at, status, instruction)
            VALUES ('ciclo-1', 'meu-epico', 'core', '2026-05-16T10:00:00Z',
                    'completo', 'instrução')
            """
        )
        patch_ciclo(conn, "ciclo-1", assertiveness_score=4, review_note="bom trabalho")

        _enrich_epicos_closure(conn, tooling)

        row = conn.execute(
            "SELECT assertiveness_score, review_note FROM ciclos WHERE id='ciclo-1'"
        ).fetchone()
    assert row["assertiveness_score"] == 4
    assert row["review_note"] == "bom trabalho"


# ── Archive fallback (v0.11.0+) ───────────────────────────────────────────────


def test_archive_fallback_fecha_epico_quando_briefing_arquivado(
    db_path: Path, tooling: Path
) -> None:
    """Briefing ausente em intents/ + presente em archive com ✅ → fecha."""
    archived = _write_archived_briefing(
        tooling, "meu-epico", "# Épico\n\nStatus: ✅ fechado\n"
    )
    archive_mtime = archived.stat().st_mtime

    with get_conn(db_path) as conn:
        _insert_epico(conn, slug="meu-epico")
        _enrich_epicos_closure(conn, tooling)
        row = conn.execute(
            "SELECT status, closed_at FROM epicos WHERE id='meu-epico'"
        ).fetchone()

    assert row["status"] == "fechado"
    assert row["closed_at"] is not None
    # closed_at deriva do mtime do arquivo (aproximação).
    expected = (
        datetime.fromtimestamp(archive_mtime, UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    assert row["closed_at"] == expected


def test_archive_fallback_briefing_ausente_em_ambos_permanece_aberto(
    db_path: Path, tooling: Path
) -> None:
    """Sem briefing em intents/ nem em archive → permanece aberto (regressão)."""
    with get_conn(db_path) as conn:
        _insert_epico(conn, slug="meu-epico")
        _enrich_epicos_closure(conn, tooling)
        row = conn.execute(
            "SELECT status, closed_at FROM epicos WHERE id='meu-epico'"
        ).fetchone()

    assert row["status"] == "aberto"
    assert row["closed_at"] is None


def test_archive_fallback_multiplos_arquivos_pega_mais_recente(
    db_path: Path, tooling: Path
) -> None:
    """Mesmo slug em múltiplos runs do archive → mtime mais recente vence."""
    old_path = _write_archived_briefing(
        tooling,
        "meu-epico",
        "# Épico\n\nStatus: ⏳ em execução\n",
        run_id="2026-05-15T01-11-34Z",
        date_prefix="2026-05-15",
        mtime=1_700_000_000.0,
    )
    new_path = _write_archived_briefing(
        tooling,
        "meu-epico",
        "# Épico\n\nStatus: ✅ fechado\n",
        run_id="2026-05-16T17-49-09Z",
        date_prefix="2026-05-16",
        mtime=1_700_500_000.0,
    )
    assert old_path.stat().st_mtime < new_path.stat().st_mtime

    with get_conn(db_path) as conn:
        _insert_epico(conn, slug="meu-epico")
        _enrich_epicos_closure(conn, tooling)
        row = conn.execute(
            "SELECT status, closed_at FROM epicos WHERE id='meu-epico'"
        ).fetchone()

    assert row["status"] == "fechado"
    expected = (
        datetime.fromtimestamp(
            new_path.stat().st_mtime,
            UTC,
        )
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    assert row["closed_at"] == expected


def test_archive_fallback_intents_vence_archive(
    db_path: Path, tooling: Path
) -> None:
    """intents/ é fonte primária — archive só é consultado se intents/ ausente."""
    # intents/ sem ✅, archive com ✅ → não deve fechar (intents/ vence).
    _write_briefing(tooling, "meu-epico", "# Épico\n\nStatus: 🎯 ativo\n")
    _write_archived_briefing(
        tooling, "meu-epico", "# Épico\n\nStatus: ✅ fechado\n"
    )

    with get_conn(db_path) as conn:
        _insert_epico(conn, slug="meu-epico")
        _enrich_epicos_closure(conn, tooling)
        row = conn.execute(
            "SELECT status, closed_at FROM epicos WHERE id='meu-epico'"
        ).fetchone()

    assert row["status"] == "aberto"
    assert row["closed_at"] is None


def test_archive_fallback_idempotente(db_path: Path, tooling: Path) -> None:
    """Rodar reconcile 2x com fallback do archive → closed_at não regride."""
    _write_archived_briefing(
        tooling, "meu-epico", "# Épico\n\nStatus: ✅ fechado\n"
    )

    with get_conn(db_path) as conn:
        _insert_epico(conn, slug="meu-epico")
        _enrich_epicos_closure(conn, tooling)
        first = conn.execute(
            "SELECT status, closed_at FROM epicos WHERE id='meu-epico'"
        ).fetchone()
        _enrich_epicos_closure(conn, tooling)
        second = conn.execute(
            "SELECT status, closed_at FROM epicos WHERE id='meu-epico'"
        ).fetchone()

    assert first["status"] == "fechado"
    assert second["status"] == "fechado"
    assert first["closed_at"] == second["closed_at"]


def test_load_archived_briefing_helper(tooling: Path) -> None:
    """Helper retorna (texto, mtime_iso) ou (None, None) quando ausente."""
    assert load_archived_briefing(tooling, "ausente") == (None, None)

    path = _write_archived_briefing(
        tooling, "presente", "# Conteúdo\n\nStatus: ✅\n"
    )
    text, mtime_iso = load_archived_briefing(tooling, "presente")
    assert text is not None
    assert "Status: ✅" in text
    expected = (
        datetime.fromtimestamp(
            path.stat().st_mtime,
            UTC,
        )
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    assert mtime_iso == expected


# ── Regressão: epicos.intencao continua sendo preenchido ─────────────────────


def test_intencao_continua_preenchida_apos_closure(db_path: Path, tooling: Path) -> None:
    """_enrich_epicos_closure não regride o preenchimento de epicos.intencao."""
    (tooling / "intents").mkdir(parents=True, exist_ok=True)
    dir_ = tooling / "intents" / "2026-05-16-meu-epico"
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / "master-briefing.md").write_text(
        "# feat: título do épico\n\nStatus: ✅ fechado\n",
        encoding="utf-8",
    )

    def _fi(_owner, repo, **_kw):
        return []

    def _fp(_owner, repo, **_kw):
        return []

    reconcile(
        db_path=str(db_path),
        fetch_issues_fn=_fi,
        fetch_prs_fn=_fp,
        repos=("mmb-core",),
        andaime_version_fn=lambda: None,
        tooling_root=str(tooling),
        briefings=[],
        journal_signals=[],
        agent_signals=[],
        now_epoch=datetime.fromisoformat("2026-05-16T20:00:00+00:00").timestamp(),
        targets_for_sync=[],
    )

    # Nenhuma issue → nenhum épico criado pelo reconciler.
    # Testamos intencao + closure diretamente via conexão.
    with get_conn(db_path) as conn:
        _insert_epico(conn, slug="meu-epico")
        # Simula estado pós-fase-1 (intencao = slug placeholder)
        assert conn.execute(
            "SELECT intencao FROM epicos WHERE id='meu-epico'"
        ).fetchone()["intencao"] == "meu-epico"

        from mmb_logger.reconcile.reconcile import _enrich_epicos_intencao
        _enrich_epicos_intencao(conn, tooling)
        _enrich_epicos_closure(conn, tooling)

        row = conn.execute(
            "SELECT intencao, status FROM epicos WHERE id='meu-epico'"
        ).fetchone()

    assert row["intencao"] == "feat: título do épico"
    assert row["status"] == "fechado"
