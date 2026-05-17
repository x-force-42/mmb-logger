"""Camada de acesso ao SQLite.

SQL puro, sem ORM. Conexão configurada com `row_factory = sqlite3.Row`
e `foreign_keys = ON`.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path("./mmb-logger.db")
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema.sql"


def now_iso() -> str:
    """Timestamp ISO8601 UTC com sufixo Z."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_db_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve caminho do DB: argumento > env MMB_LOGGER_DB_PATH > default."""
    if explicit:
        return Path(explicit)
    env = os.environ.get("MMB_LOGGER_DB_PATH")
    if env:
        return Path(env)
    return DEFAULT_DB_PATH


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_conn(db_path: str | os.PathLike[str] | None = None) -> Iterator[sqlite3.Connection]:
    path = resolve_db_path(db_path)
    conn = _connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str | os.PathLike[str] | None = None) -> Path:
    """Aplica schema.sql. Idempotente (todos os CREATEs usam IF NOT EXISTS).

    Também migra colunas e constraints adicionadas após o schema inicial:
    - ALTER TABLE defensivo (ignora se coluna já existir).
    - Recriação de `ciclos` se CHECK de `abort_origin` ainda for v1.
    """
    path = resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with _connect(path) as conn:
        conn.executescript(sql)
        # Migração defensiva: colunas adicionadas após v1 do schema.
        for stmt in (
            "ALTER TABLE epicos ADD COLUMN andaime_version TEXT",
            "ALTER TABLE ciclos ADD COLUMN andaime_version TEXT",
            "ALTER TABLE eventos ADD COLUMN source_key TEXT",
        ):
            try:
                conn.execute(stmt)
            except Exception:
                pass  # coluna já existe — idempotente
        # UNIQUE parcial em eventos.source_key (executescript de schema.sql
        # cobre, mas explicitamos pra casos de DB pré-existente sem schema reload)
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_eventos_source_key "
                "ON eventos(source_key) WHERE source_key IS NOT NULL"
            )
        except Exception:
            pass
        _migrate_ciclos_abort_origin_v2(conn)
        conn.commit()
    return path


def _migrate_ciclos_abort_origin_v2(conn: sqlite3.Connection) -> None:
    """Estende CHECK de `ciclos.abort_origin` pra incluir worker-exit/timeout/stale.

    SQLite não suporta ALTER TABLE DROP/MODIFY CONSTRAINT, então o
    procedimento é o "table-rebuild dance":
      1. detectar via `sqlite_master.sql` se a CHECK antiga ainda vigora;
      2. PRAGMA foreign_keys=OFF (preserva rows em `eventos` durante o drop);
      3. CREATE TABLE ciclos_new com a nova CHECK;
      4. INSERT INTO ciclos_new SELECT * FROM ciclos;
      5. DROP TABLE ciclos; ALTER TABLE ciclos_new RENAME TO ciclos;
      6. recriar índices (perdidos no DROP);
      7. PRAGMA foreign_keys=ON + foreign_key_check pra sanidade.

    Idempotente: se a CHECK nova já estiver no sqlite_master, no-op.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='ciclos'"
    ).fetchone()
    if row is None:
        return  # tabela ainda não existe; schema.sql vai criar com a CHECK nova
    existing_sql = row["sql"] or ""
    if "'worker-exit'" in existing_sql:
        return  # já migrada

    # Lê o schema canônico do arquivo pra extrair o CREATE TABLE de ciclos.
    full_schema = SCHEMA_PATH.read_text(encoding="utf-8")
    new_ciclos_ddl = _extract_create_ciclos(full_schema)
    if new_ciclos_ddl is None:
        raise RuntimeError(
            "migration: não consegui extrair CREATE TABLE ciclos de schema.sql"
        )
    new_ciclos_ddl = new_ciclos_ddl.replace(
        "CREATE TABLE IF NOT EXISTS ciclos",
        "CREATE TABLE ciclos_new",
    )

    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN")
        conn.executescript(new_ciclos_ddl)
        conn.execute("INSERT INTO ciclos_new SELECT * FROM ciclos")
        conn.execute("DROP TABLE ciclos")
        conn.execute("ALTER TABLE ciclos_new RENAME TO ciclos")
        # Recria índices (foram dropados junto com a tabela)
        for stmt in (
            "CREATE INDEX IF NOT EXISTS idx_ciclos_epico ON ciclos(epico_id)",
            "CREATE INDEX IF NOT EXISTS idx_ciclos_status ON ciclos(status)",
            "CREATE INDEX IF NOT EXISTS idx_ciclos_project ON ciclos(project)",
            "CREATE INDEX IF NOT EXISTS idx_ciclos_planner_invoked_at "
            "ON ciclos(planner_invoked_at)",
        ):
            conn.execute(stmt)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")

    # Sanidade pós-migração: nenhuma FK violada.
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(
            f"migration deixou {len(violations)} violações de FK: {violations}"
        )


def _extract_create_ciclos(schema_sql: str) -> str | None:
    """Extrai o bloco `CREATE TABLE IF NOT EXISTS ciclos (...);` do schema.sql."""
    marker = "CREATE TABLE IF NOT EXISTS ciclos"
    start = schema_sql.find(marker)
    if start == -1:
        return None
    # Acha o `);` que fecha o CREATE TABLE.
    end = schema_sql.find(");", start)
    if end == -1:
        return None
    return schema_sql[start : end + 2]


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------


def _row_to_epico(row: sqlite3.Row, stats: dict[str, int] | None = None) -> dict[str, Any]:
    stats = stats or {}
    return {
        "id": row["id"],
        "slug": row["slug"],
        "started_at": row["started_at"],
        "intencao": row["intencao"],
        "status": row["status"],
        "closed_at": row["closed_at"],
        "andaime_version": row["andaime_version"],
        "ciclos_total": stats.get("total", 0),
        "ciclos_completos": stats.get("completos", 0),
        "ciclos_abortados": stats.get("abortados", 0),
    }


def _row_to_ciclo(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "epico_id": row["epico_id"],
        "project": row["project"],
        "planner_invoked_at": row["planner_invoked_at"],
        "status": row["status"],
        "instruction": row["instruction"],
        "pr_url": row["pr_url"],
        "pr_number": row["pr_number"],
        "closed_partial_at": row["closed_partial_at"],
        "closed_complete_at": row["closed_complete_at"],
        "merged_to_main": row["merged_to_main"],
        "assertiveness_score": row["assertiveness_score"],
        "cost_usd": row["cost_usd"],
        "abort_origin": row["abort_origin"],
        "abort_reason": row["abort_reason"],
        "andaime_version": row["andaime_version"],
    }


def _row_to_ciclo_detail(row: sqlite3.Row) -> dict[str, Any]:
    base = _row_to_ciclo(row)
    base.update(
        {
            "briefing_md": row["briefing_md"],
            "review_note": row["review_note"],
            "abort_at": row["abort_at"],
            "tokens_input": row["tokens_input"],
            "tokens_output": row["tokens_output"],
            "diff_added": row["diff_added"],
            "diff_deleted": row["diff_deleted"],
            "diff_files": row["diff_files"],
        }
    )
    return base


def _row_to_evento(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
    except json.JSONDecodeError:
        payload = {"_raw": row["payload_json"]}
    return {
        "id": row["id"],
        "ciclo_id": row["ciclo_id"],
        "ts": row["ts"],
        "kind": row["kind"],
        "severity": row["severity"],
        "payload": payload,
    }


def _row_to_projeto(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "slug": row["slug"],
        "name": row["name"],
        "path": row["path"],
        "repo_url": row["repo_url"],
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------------------
# Épicos
# ---------------------------------------------------------------------------


def upsert_epico(
    conn: sqlite3.Connection,
    *,
    id: str,
    slug: str,
    started_at: str,
    intencao: str,
    status: str = "aberto",
    andaime_version: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO epicos (id, slug, started_at, intencao, status, andaime_version)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (id, slug, started_at, intencao, status, andaime_version),
    )


def get_epico(conn: sqlite3.Connection, epico_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM epicos WHERE id = ?", (epico_id,)).fetchone()
    if not row:
        return None
    stats = _epico_stats(conn, epico_id)
    return _row_to_epico(row, stats)


def _epico_stats(conn: sqlite3.Connection, epico_id: str) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN status = 'completo' THEN 1 ELSE 0 END) AS completos,
          SUM(CASE WHEN status = 'abortado' THEN 1 ELSE 0 END) AS abortados
        FROM ciclos WHERE epico_id = ?
        """,
        (epico_id,),
    ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "completos": int(row["completos"] or 0),
        "abortados": int(row["abortados"] or 0),
    }


def list_epicos(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    andaime_versions: list[str] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    where: list[str] = []
    params: list[Any] = []
    if status:
        where.append("status = ?")
        params.append(status)
    if date_from:
        where.append("started_at >= ?")
        params.append(date_from)
    if date_to:
        where.append("started_at <= ?")
        params.append(date_to)
    if andaime_versions:
        placeholders = ",".join("?" for _ in andaime_versions)
        where.append(f"andaime_version IN ({placeholders})")
        params.extend(andaime_versions)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) AS n FROM epicos {clause}", params).fetchone()["n"]
    rows = conn.execute(
        f"""
        SELECT * FROM epicos
        {clause}
        ORDER BY started_at DESC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()
    items = [_row_to_epico(r, _epico_stats(conn, r["id"])) for r in rows]
    return items, int(total)


# ---------------------------------------------------------------------------
# Ciclos
# ---------------------------------------------------------------------------


def upsert_ciclo(
    conn: sqlite3.Connection,
    *,
    id: str,
    epico_id: str,
    project: str,
    planner_invoked_at: str,
    status: str,
    instruction: str,
    briefing_md: str | None = None,
    andaime_version: str | None = None,
) -> bool:
    """Insere ciclo se ainda não existe. Retorna True se inseriu."""
    cur = conn.execute(
        """
        INSERT INTO ciclos
          (id, epico_id, project, planner_invoked_at, status, instruction, briefing_md,
           andaime_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (id, epico_id, project, planner_invoked_at, status, instruction, briefing_md,
         andaime_version),
    )
    return cur.rowcount > 0


def get_ciclo(conn: sqlite3.Connection, ciclo_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM ciclos WHERE id = ?", (ciclo_id,)).fetchone()
    return _row_to_ciclo_detail(row) if row else None


def update_ciclo_status(
    conn: sqlite3.Connection,
    ciclo_id: str,
    *,
    status: str,
    closed_partial_at: str | None = None,
    closed_complete_at: str | None = None,
    pr_number: int | None = None,
    pr_url: str | None = None,
    abort_at: str | None = None,
    abort_origin: str | None = None,
    abort_reason: str | None = None,
) -> None:
    sets: list[str] = ["status = ?"]
    params: list[Any] = [status]
    if closed_partial_at is not None:
        sets.append("closed_partial_at = ?")
        params.append(closed_partial_at)
    if closed_complete_at is not None:
        sets.append("closed_complete_at = ?")
        params.append(closed_complete_at)
    if pr_number is not None:
        sets.append("pr_number = ?")
        params.append(pr_number)
    if pr_url is not None:
        sets.append("pr_url = ?")
        params.append(pr_url)
    if abort_at is not None:
        sets.append("abort_at = ?")
        params.append(abort_at)
    if abort_origin is not None:
        sets.append("abort_origin = ?")
        params.append(abort_origin)
    if abort_reason is not None:
        sets.append("abort_reason = ?")
        params.append(abort_reason)
    params.append(ciclo_id)
    conn.execute(f"UPDATE ciclos SET {', '.join(sets)} WHERE id = ?", params)


def patch_ciclo(
    conn: sqlite3.Connection,
    ciclo_id: str,
    *,
    merged_to_main: int | None = None,
    assertiveness_score: int | None = None,
    review_note: str | None = None,
) -> bool:
    sets: list[str] = []
    params: list[Any] = []
    if merged_to_main is not None:
        sets.append("merged_to_main = ?")
        params.append(merged_to_main)
    if assertiveness_score is not None:
        sets.append("assertiveness_score = ?")
        params.append(assertiveness_score)
    if review_note is not None:
        sets.append("review_note = ?")
        params.append(review_note)
    if not sets:
        return False
    params.append(ciclo_id)
    cur = conn.execute(f"UPDATE ciclos SET {', '.join(sets)} WHERE id = ?", params)
    return cur.rowcount > 0


def list_ciclos(
    conn: sqlite3.Connection,
    *,
    epico: str | None = None,
    project: str | None = None,
    status: str | None = None,
    abort_origin: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    andaime_versions: list[str] | None = None,
    order_by: str = "planner_invoked_at",
    order_dir: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    where: list[str] = []
    params: list[Any] = []
    if epico:
        where.append("epico_id = ?")
        params.append(epico)
    if project:
        where.append("project = ?")
        params.append(project)
    if status:
        where.append("status = ?")
        params.append(status)
    if abort_origin:
        where.append("abort_origin = ?")
        params.append(abort_origin)
    if date_from:
        where.append("planner_invoked_at >= ?")
        params.append(date_from)
    if date_to:
        where.append("planner_invoked_at <= ?")
        params.append(date_to)
    if andaime_versions:
        placeholders = ",".join("?" for _ in andaime_versions)
        where.append(f"andaime_version IN ({placeholders})")
        params.extend(andaime_versions)

    allowed_order = {"planner_invoked_at", "cost_usd"}
    if order_by not in allowed_order:
        order_by = "planner_invoked_at"
    direction = "DESC" if order_dir.lower() == "desc" else "ASC"
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    total = conn.execute(f"SELECT COUNT(*) AS n FROM ciclos {clause}", params).fetchone()["n"]
    rows = conn.execute(
        f"""
        SELECT * FROM ciclos
        {clause}
        ORDER BY {order_by} {direction}
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()
    return [_row_to_ciclo(r) for r in rows], int(total)


def list_ciclos_by_epico(conn: sqlite3.Connection, epico_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM ciclos WHERE epico_id = ? ORDER BY planner_invoked_at DESC",
        (epico_id,),
    ).fetchall()
    return [_row_to_ciclo(r) for r in rows]


def find_ciclo_for_transition(
    conn: sqlite3.Connection,
    *,
    epico_id: str,
    project: str,
    accepted_statuses: list[str],
) -> dict[str, Any] | None:
    """Acha o ciclo mais recente do par (épico, projeto) num dos status aceitos.

    Usado pelas regras de inferência R2-R5.
    """
    placeholders = ",".join("?" for _ in accepted_statuses)
    row = conn.execute(
        f"""
        SELECT * FROM ciclos
        WHERE epico_id = ? AND project = ? AND status IN ({placeholders})
        ORDER BY planner_invoked_at DESC
        LIMIT 1
        """,
        (epico_id, project, *accepted_statuses),
    ).fetchone()
    return _row_to_ciclo_detail(row) if row else None


def find_latest_open_ciclo_by_project(
    conn: sqlite3.Connection, *, project: str
) -> dict[str, Any] | None:
    """Heurística R6/R7: último ciclo aberto do projeto (sem task_id explícito)."""
    row = conn.execute(
        """
        SELECT * FROM ciclos
        WHERE project = ? AND status IN ('iniciado', 'planejado', 'pr_aberto')
        ORDER BY planner_invoked_at DESC
        LIMIT 1
        """,
        (project,),
    ).fetchone()
    return _row_to_ciclo_detail(row) if row else None


# ---------------------------------------------------------------------------
# Eventos
# ---------------------------------------------------------------------------


def insert_evento(
    conn: sqlite3.Connection,
    *,
    ciclo_id: str | None,
    ts: str,
    kind: str,
    severity: str | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    payload_json = json.dumps(payload or {}, ensure_ascii=False, default=str)
    cur = conn.execute(
        """
        INSERT INTO eventos (ciclo_id, ts, kind, severity, payload_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (ciclo_id, ts, kind, severity, payload_json),
    )
    return int(cur.lastrowid)


def list_eventos_by_ciclo(conn: sqlite3.Connection, ciclo_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM eventos WHERE ciclo_id = ? ORDER BY ts ASC, id ASC",
        (ciclo_id,),
    ).fetchall()
    return [_row_to_evento(r) for r in rows]


# ---------------------------------------------------------------------------
# Projetos
# ---------------------------------------------------------------------------


def upsert_projeto(
    conn: sqlite3.Connection,
    *,
    id: str,
    slug: str,
    name: str,
    path: str,
    repo_url: str | None = None,
    created_at: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO projetos (id, slug, name, path, repo_url, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          name = excluded.name,
          path = excluded.path,
          repo_url = excluded.repo_url
        """,
        (id, slug, name, path, repo_url, created_at or now_iso()),
    )


def list_projetos(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM projetos ORDER BY slug").fetchall()
    return [_row_to_projeto(r) for r in rows]


# ---------------------------------------------------------------------------
# Counts (health/detailed)
# ---------------------------------------------------------------------------


def count_ciclos(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM ciclos").fetchone()[0])


def count_projetos(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM projetos").fetchone()[0])


def count_eventos(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM eventos").fetchone()[0])


def _semver_key(v: str) -> tuple[int, ...]:
    """Parse 'v0.7.0' -> (0,7,0), 'v0.1' -> (0,1), 'v0' -> (0,).

    Tolerante a sufixos não-numéricos (ex.: 'v1.0.0-rc1' -> (1,0,0));
    para a parte não-parseável, interrompe e mantém os componentes
    inteiros já acumulados.
    """
    s = v.lstrip("v")
    parts: list[int] = []
    for p in s.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            break
    return tuple(parts) if parts else (0,)


def list_andaime_versions(conn: sqlite3.Connection) -> list[str]:
    """Versões distintas presentes em ciclos OU épicos, semver-desc, sem NULL."""
    rows = conn.execute(
        """
        SELECT DISTINCT andaime_version FROM (
            SELECT andaime_version FROM ciclos WHERE andaime_version IS NOT NULL
            UNION
            SELECT andaime_version FROM epicos WHERE andaime_version IS NOT NULL
        )
        """
    ).fetchall()
    versions = [r[0] for r in rows]
    return sorted(versions, key=_semver_key, reverse=True)


# ---------------------------------------------------------------------------
# Estado de ingestão
# ---------------------------------------------------------------------------


def is_file_processed(conn: sqlite3.Connection, path: str) -> bool:
    row = conn.execute("SELECT 1 FROM processed_files WHERE path = ?", (path,)).fetchone()
    return row is not None


def mark_file_processed(conn: sqlite3.Connection, path: str) -> None:
    conn.execute(
        """
        INSERT INTO processed_files (path, processed_at)
        VALUES (?, ?)
        ON CONFLICT(path) DO NOTHING
        """,
        (path, now_iso()),
    )


def get_jsonl_offset(conn: sqlite3.Connection, source: str) -> int:
    row = conn.execute(
        "SELECT last_offset FROM jsonl_cursor WHERE source = ?", (source,)
    ).fetchone()
    return int(row["last_offset"]) if row else 0


def set_jsonl_offset(conn: sqlite3.Connection, source: str, offset: int) -> None:
    conn.execute(
        """
        INSERT INTO jsonl_cursor (source, last_offset, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(source) DO UPDATE SET
          last_offset = excluded.last_offset,
          updated_at = excluded.updated_at
        """,
        (source, offset, now_iso()),
    )


# ---------------------------------------------------------------------------
# Métricas
# ---------------------------------------------------------------------------


def metrics_overview(conn: sqlite3.Connection, days: int = 30) -> dict[str, Any]:
    cutoff = datetime.now(UTC).timestamp() - days * 86400
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    ciclos_total = conn.execute(
        "SELECT COUNT(*) AS n FROM ciclos WHERE planner_invoked_at >= ?", (cutoff_iso,)
    ).fetchone()["n"]
    epicos_total = conn.execute(
        "SELECT COUNT(*) AS n FROM epicos WHERE started_at >= ?", (cutoff_iso,)
    ).fetchone()["n"]
    custo_total = conn.execute(
        """
        SELECT COALESCE(SUM(cost_usd), 0.0) AS c FROM ciclos
        WHERE planner_invoked_at >= ?
        """,
        (cutoff_iso,),
    ).fetchone()["c"]

    # Tempo médio em segundos pra ciclos completos.
    rows_completos = conn.execute(
        """
        SELECT planner_invoked_at, closed_complete_at FROM ciclos
        WHERE status = 'completo'
          AND planner_invoked_at >= ?
          AND closed_complete_at IS NOT NULL
        """,
        (cutoff_iso,),
    ).fetchall()
    durations: list[float] = []
    for r in rows_completos:
        try:
            t0 = datetime.fromisoformat(r["planner_invoked_at"].replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(r["closed_complete_at"].replace("Z", "+00:00"))
            durations.append((t1 - t0).total_seconds())
        except (ValueError, TypeError):
            continue
    tempo_medio = sum(durations) / len(durations) if durations else 0.0

    abort_count = conn.execute(
        """
        SELECT COUNT(*) AS n FROM ciclos
        WHERE status = 'abortado' AND planner_invoked_at >= ?
        """,
        (cutoff_iso,),
    ).fetchone()["n"]
    merged_count = conn.execute(
        """
        SELECT COUNT(*) AS n FROM ciclos
        WHERE merged_to_main = 1 AND planner_invoked_at >= ?
        """,
        (cutoff_iso,),
    ).fetchone()["n"]

    taxa_abort = (abort_count / ciclos_total) if ciclos_total else 0.0
    taxa_merged = (merged_count / ciclos_total) if ciclos_total else 0.0

    # Bucketing diário em BRT (UTC-3) - storage continua UTC, mas
    # /api/metricas/overview reporta "dia operacional local" do MMB.
    # Sem o modifier, eventos rodados entre 21:00-23:59 BRT caem no
    # dia UTC seguinte e aparecem no bucket errado pro operador.
    # Decisão MVP: hardcode `-3 hours`. Promover pra env var se DST
    # voltar no Brasil ou se a operação ficar multi-timezone.
    custo_dia = conn.execute(
        """
        SELECT substr(datetime(planner_invoked_at, '-3 hours'), 1, 10) AS dia,
               COALESCE(SUM(cost_usd), 0.0) AS usd
        FROM ciclos
        WHERE planner_invoked_at >= ?
        GROUP BY dia
        ORDER BY dia
        """,
        (cutoff_iso,),
    ).fetchall()
    ciclos_dia = conn.execute(
        """
        SELECT substr(datetime(planner_invoked_at, '-3 hours'), 1, 10) AS dia,
               COUNT(*) AS n
        FROM ciclos
        WHERE planner_invoked_at >= ?
        GROUP BY dia
        ORDER BY dia
        """,
        (cutoff_iso,),
    ).fetchall()

    status_rows = conn.execute(
        """
        SELECT status, COUNT(*) AS n FROM ciclos
        WHERE planner_invoked_at >= ?
        GROUP BY status
        """,
        (cutoff_iso,),
    ).fetchall()
    status_breakdown = {
        s: 0 for s in ("iniciado", "planejado", "pr_aberto", "completo", "abortado")
    }
    for r in status_rows:
        status_breakdown[r["status"]] = int(r["n"])

    abort_rows = conn.execute(
        """
        SELECT abort_origin, COUNT(*) AS n FROM ciclos
        WHERE abort_origin IS NOT NULL AND planner_invoked_at >= ?
        GROUP BY abort_origin
        """,
        (cutoff_iso,),
    ).fetchall()
    abort_breakdown = {o: 0 for o in ("heartbeat", "manual", "self", "master")}
    for r in abort_rows:
        abort_breakdown[r["abort_origin"]] = int(r["n"])

    return {
        "window_days": days,
        "ciclos_total": int(ciclos_total),
        "epicos_total": int(epicos_total),
        "custo_total_usd": float(custo_total),
        "tempo_medio_completo_s": float(tempo_medio),
        "taxa_abort": float(taxa_abort),
        "taxa_merged": float(taxa_merged),
        "custo_por_dia": [{"dia": r["dia"], "usd": float(r["usd"])} for r in custo_dia],
        "ciclos_por_dia": [{"dia": r["dia"], "n": int(r["n"])} for r in ciclos_dia],
        "status_breakdown": status_breakdown,
        "abort_breakdown": abort_breakdown,
    }
