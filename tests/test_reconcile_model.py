"""Testes da captura de `ciclos.model` a partir de `state/agents.jsonl`.

Cobertura:
- Parser de `model` em eventos spawn (presente/ausente).
- `load_planner_models`: só planners (id sem dash), primeiro spawn vence,
  atômicos ignorados, ausência de modelo ignorada.
- Reconcile end-to-end: `ciclos.model` preenchido quando agents.jsonl tem
  spawn do planner com modelo; permanece NULL quando ausente; preservação
  de coluna humana através de runs adicionais.
- API: `GET /api/ciclos` expõe `model` no payload.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from mmb_logger.db import get_conn, patch_ciclo
from mmb_logger.ingest.agents_stream import parse_line as parse_agent_line
from mmb_logger.reconcile.gh import GhIssue
from mmb_logger.reconcile.planner_models import load_planner_models
from mmb_logger.reconcile.reconcile import reconcile

# ── Helpers compartilhados ────────────────────────────────────────


def _issue(
    *,
    repo: str = "mmb-core",
    number: int = 1,
    labels: tuple[str, ...] = ("task", "project:mmb-core", "epic:e1"),
    body: str = "",
    state: str = "OPEN",
    created_at: str = "2026-05-16T00:00:00Z",
) -> GhIssue:
    return GhIssue(
        repo=repo,
        number=number,
        title="t",
        body=body,
        state=state,
        labels=labels,
        created_at=created_at,
        closed_at=None,
    )


def _spawn_line(
    *,
    ts: str,
    agent_id: str,
    epic: str,
    model: str | None = None,
    pid: int = 1234,
) -> str:
    d: dict = {"ts": ts, "ev": "spawn", "id": agent_id, "pid": pid, "epic": epic}
    if model is not None:
        d["model"] = model
    return json.dumps(d) + "\n"


def _seed_agents_jsonl(tooling_root: Path, lines: list[str]) -> None:
    state = tooling_root / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "agents.jsonl").write_text("".join(lines), encoding="utf-8")


def _run(
    db_path: Path,
    tooling_root: Path,
    *,
    issues: dict[str, list[GhIssue]] | None = None,
    repos: tuple[str, ...] = ("mmb-core",),
):
    def fi(_owner, repo, **_kw):
        return (issues or {}).get(repo, [])

    def fp(_owner, _repo, **_kw):
        return []

    return reconcile(
        db_path=str(db_path),
        fetch_issues_fn=fi,
        fetch_prs_fn=fp,
        repos=repos,
        andaime_version_fn=lambda: None,
        tooling_root=str(tooling_root),
        claude_projects_root=str(tooling_root),  # sem transcripts reais
        briefings=[],
        briefings_malformed=[],
        journal_signals=[],
        agent_signals=[],
        now_epoch=0.0,
    )


# ── Parser ─────────────────────────────────────────────────────────


def test_parse_line_extrai_model_de_spawn():
    e = parse_agent_line(
        '{"ts":"t","ev":"spawn","id":"core","epic":"e1","model":"claude-opus-4-7"}'
    )
    assert e is not None
    assert e.model == "claude-opus-4-7"


def test_parse_line_model_ausente_vira_none():
    e = parse_agent_line('{"ts":"t","ev":"spawn","id":"core","epic":"e1"}')
    assert e is not None
    assert e.model is None


def test_parse_line_model_vazio_vira_none():
    e = parse_agent_line(
        '{"ts":"t","ev":"spawn","id":"core","epic":"e1","model":""}'
    )
    assert e is not None
    assert e.model is None


# ── load_planner_models ───────────────────────────────────────────


def test_load_planner_models_basico(tooling_root: Path):
    _seed_agents_jsonl(
        tooling_root,
        [
            _spawn_line(
                ts="2026-05-16T10:00:00Z",
                agent_id="core",
                epic="e1",
                model="claude-opus-4-7",
            ),
        ],
    )
    out = load_planner_models(tooling_root)
    assert out == {("e1", "core"): "claude-opus-4-7"}


def test_load_planner_models_ignora_atomicos(tooling_root: Path):
    """Atômicos (id com dash) têm modelo próprio, mas não populam ciclos.model."""
    _seed_agents_jsonl(
        tooling_root,
        [
            _spawn_line(
                ts="t1",
                agent_id="core-X1",
                epic="e1",
                model="claude-sonnet-4-6",
            ),
        ],
    )
    out = load_planner_models(tooling_root)
    assert out == {}


def test_load_planner_models_primeiro_spawn_vence(tooling_root: Path):
    """Múltiplos spawns do mesmo planner pro mesmo épico: 1º com modelo vence."""
    _seed_agents_jsonl(
        tooling_root,
        [
            _spawn_line(ts="t1", agent_id="core", epic="e1", model="claude-opus-4-7"),
            _spawn_line(ts="t2", agent_id="core", epic="e1", model="claude-haiku-4-5"),
        ],
    )
    out = load_planner_models(tooling_root)
    assert out == {("e1", "core"): "claude-opus-4-7"}


def test_load_planner_models_ignora_sem_modelo(tooling_root: Path):
    """Spawn sem `model` é ignorado (tolerância — sem warn, sem erro)."""
    _seed_agents_jsonl(
        tooling_root,
        [
            _spawn_line(ts="t1", agent_id="core", epic="e1"),  # sem model
            _spawn_line(ts="t2", agent_id="core", epic="e1", model="claude-opus-4-7"),
        ],
    )
    out = load_planner_models(tooling_root)
    assert out == {("e1", "core"): "claude-opus-4-7"}


def test_load_planner_models_arquivo_ausente(tooling_root: Path):
    """Sem `state/agents.jsonl` → dict vazio, sem exceção."""
    # state/ existe mas sem agents.jsonl
    assert load_planner_models(tooling_root) == {}


def test_load_planner_models_ignora_deregister(tooling_root: Path):
    """Só eventos `spawn` contam — deregister/heartbeat são ignorados."""
    _seed_agents_jsonl(
        tooling_root,
        [
            json.dumps(
                {
                    "ts": "t1",
                    "ev": "deregister",
                    "id": "core",
                    "epic": "e1",
                    "model": "claude-opus-4-7",
                }
            )
            + "\n",
        ],
    )
    assert load_planner_models(tooling_root) == {}


# ── Reconcile end-to-end ──────────────────────────────────────────


def test_reconcile_popula_ciclo_model_via_agents_jsonl(
    db_path: Path, tooling_root: Path
):
    body = "<!-- mmb-cycle-key: e1/core/2026-05-16T00:00:00Z -->\n# briefing"
    _seed_agents_jsonl(
        tooling_root,
        [
            _spawn_line(
                ts="2026-05-16T00:01:00Z",
                agent_id="core",
                epic="e1",
                model="claude-opus-4-7",
            ),
        ],
    )
    _run(
        db_path,
        tooling_root,
        issues={"mmb-core": [_issue(body=body)]},
    )
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT model FROM ciclos").fetchone()
        assert row["model"] == "claude-opus-4-7"


def test_reconcile_model_null_quando_sem_spawn(
    db_path: Path, tooling_root: Path
):
    """Sem agents.jsonl ou sem spawn pro par (epic, project) → model permanece NULL."""
    body = "<!-- mmb-cycle-key: e1/core/2026-05-16T00:00:00Z -->\n# briefing"
    # state/agents.jsonl ausente — tooling_root tem state/ vazia
    _run(
        db_path,
        tooling_root,
        issues={"mmb-core": [_issue(body=body)]},
    )
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT model FROM ciclos").fetchone()
        assert row["model"] is None


def test_reconcile_atomico_nao_polui_model(db_path: Path, tooling_root: Path):
    """Só planner (id sem dash) preenche ciclos.model. Atômico isolado → NULL."""
    body = "<!-- mmb-cycle-key: e1/core/2026-05-16T00:00:00Z -->"
    _seed_agents_jsonl(
        tooling_root,
        [
            _spawn_line(
                ts="t1",
                agent_id="core-X1",
                epic="e1",
                model="claude-sonnet-4-6",
            ),
        ],
    )
    _run(
        db_path,
        tooling_root,
        issues={"mmb-core": [_issue(body=body)]},
    )
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT model FROM ciclos").fetchone()
        assert row["model"] is None


def test_reconcile_preserva_assertiveness_quando_model_chega(
    db_path: Path, tooling_root: Path
):
    """Run 1 sem agents.jsonl: ciclo nasce sem model + user põe assertiveness=5.
    Run 2 com agents.jsonl: model populado, assertiveness preservada.
    """
    body = "<!-- mmb-cycle-key: e1/core/2026-05-16T00:00:00Z -->"
    _run(db_path, tooling_root, issues={"mmb-core": [_issue(body=body)]})
    with get_conn(db_path) as conn:
        cid = conn.execute("SELECT id FROM ciclos").fetchone()["id"]
        patch_ciclo(conn, cid, assertiveness_score=5)

    _seed_agents_jsonl(
        tooling_root,
        [
            _spawn_line(
                ts="t1",
                agent_id="core",
                epic="e1",
                model="claude-opus-4-7",
            ),
        ],
    )
    _run(db_path, tooling_root, issues={"mmb-core": [_issue(body=body)]})

    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT model, assertiveness_score FROM ciclos"
        ).fetchone()
        assert row["model"] == "claude-opus-4-7"
        assert row["assertiveness_score"] == 5


def test_reconcile_model_idempotente(db_path: Path, tooling_root: Path):
    body = "<!-- mmb-cycle-key: e1/core/2026-05-16T00:00:00Z -->"
    _seed_agents_jsonl(
        tooling_root,
        [
            _spawn_line(
                ts="t1",
                agent_id="core",
                epic="e1",
                model="claude-opus-4-7",
            ),
        ],
    )
    _run(db_path, tooling_root, issues={"mmb-core": [_issue(body=body)]})
    _run(db_path, tooling_root, issues={"mmb-core": [_issue(body=body)]})
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT id, model FROM ciclos").fetchall()
        assert len(rows) == 1
        assert rows[0]["model"] == "claude-opus-4-7"


# ── API ───────────────────────────────────────────────────────────


def test_api_lista_ciclos_expoe_model(
    client: TestClient, db_path: Path, tooling_root: Path
):
    body = "<!-- mmb-cycle-key: e1/core/2026-05-16T00:00:00Z -->"
    _seed_agents_jsonl(
        tooling_root,
        [
            _spawn_line(
                ts="t1",
                agent_id="core",
                epic="e1",
                model="claude-opus-4-7",
            ),
        ],
    )
    _run(db_path, tooling_root, issues={"mmb-core": [_issue(body=body)]})

    r = client.get("/api/ciclos")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["model"] == "claude-opus-4-7"


def test_api_detail_ciclo_expoe_model(
    client: TestClient, db_path: Path, tooling_root: Path
):
    body = "<!-- mmb-cycle-key: e1/core/2026-05-16T00:00:00Z -->"
    _seed_agents_jsonl(
        tooling_root,
        [
            _spawn_line(
                ts="t1",
                agent_id="core",
                epic="e1",
                model="claude-sonnet-4-6",
            ),
        ],
    )
    _run(db_path, tooling_root, issues={"mmb-core": [_issue(body=body)]})

    with get_conn(db_path) as conn:
        cid = conn.execute("SELECT id FROM ciclos").fetchone()["id"]
    r = client.get(f"/api/ciclos/{cid}")
    assert r.status_code == 200
    assert r.json()["model"] == "claude-sonnet-4-6"


def test_api_lista_ciclos_model_null_quando_ausente(
    client: TestClient, db_path: Path, tooling_root: Path
):
    body = "<!-- mmb-cycle-key: e1/core/2026-05-16T00:00:00Z -->"
    _run(db_path, tooling_root, issues={"mmb-core": [_issue(body=body)]})
    r = client.get("/api/ciclos")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["model"] is None
