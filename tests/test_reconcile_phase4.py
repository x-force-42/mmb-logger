"""Testes da fase 4: cost_usd, tokens_input, tokens_output via transcripts.

Cada teste cria estrutura de transcripts em tmp_path com encoding correto
da worktree e injeta no `reconcile()` via parâmetros. Nenhum teste lê o
`~/.claude/projects` real.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mmb_logger.db import get_conn, patch_ciclo
from mmb_logger.reconcile.gh import GhIssue, GhPr
from mmb_logger.reconcile.reconcile import reconcile
from mmb_logger.reconcile.transcripts import (
    PRICING,
    compute_cost,
    dominant_model,
    encode_worktree_path,
    find_transcripts,
    sum_usage_from_transcript,
)

# ── Fixtures: mmb_root, claude_projects_root, e helpers ───────────


@pytest.fixture()
def mmb_root(tmp_path: Path) -> Path:
    """Fake MMB root. mmb-core/.worktrees/<wt-name>/ é onde fica o worktree."""
    root = tmp_path / "MMB"
    (root / "mmb-core" / ".worktrees").mkdir(parents=True)
    (root / "mmb-cockpit" / ".worktrees").mkdir(parents=True)
    (root / "mmb-aquarium" / ".worktrees").mkdir(parents=True)
    return root


@pytest.fixture()
def claude_projects(tmp_path: Path) -> Path:
    root = tmp_path / "claude-projects"
    root.mkdir(parents=True)
    return root


def _make_transcript_dir(
    claude_projects: Path,
    mmb_root: Path,
    *,
    repo: str,
    wt_name: str,
) -> Path:
    """Cria o dir de transcripts pra worktree informada, com encoding correto."""
    worktree_path = str(mmb_root / repo / ".worktrees" / wt_name)
    encoded = encode_worktree_path(worktree_path)
    project_dir = claude_projects / encoded
    project_dir.mkdir(parents=True)
    return project_dir


def _write_transcript(
    project_dir: Path,
    *,
    session_uuid: str = "test-session-001",
    turns: list[dict],
) -> Path:
    """Cria <session>.jsonl com lista de turns (cada turn = dict de message)."""
    path = project_dir / f"{session_uuid}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for msg in turns:
            line = {"type": "assistant", "message": msg}
            f.write(json.dumps(line) + "\n")
    return path


def _assistant_turn(
    *,
    model: str = "claude-opus-4-7",
    input_tokens: int = 100,
    output_tokens: int = 200,
    cache_5m: int = 0,
    cache_1h: int = 0,
    cache_read: int = 0,
) -> dict:
    return {
        "type": "message",
        "model": model,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_5m + cache_1h,
            "cache_creation": {
                "ephemeral_5m_input_tokens": cache_5m,
                "ephemeral_1h_input_tokens": cache_1h,
            },
        },
    }


def _issue(
    *,
    repo: str = "mmb-core",
    number: int = 1,
    labels: tuple[str, ...] = ("task", "project:mmb-core", "epic:e1"),
    body: str = "",
    state: str = "OPEN",
    created_at: str = "2026-05-16T10:00:00Z",
    closed_at: str | None = None,
) -> GhIssue:
    return GhIssue(
        repo=repo,
        number=number,
        title="t",
        body=body,
        state=state,
        labels=labels,
        created_at=created_at,
        closed_at=closed_at,
    )


def _pr(
    *,
    number: int = 10,
    head_ref_name: str = "task/X1-foo",
    body: str = "Closes #1",
    created_at: str = "2026-05-16T11:00:00Z",
    merged_at: str | None = None,
    state: str = "OPEN",
    additions: int = 5,
    deletions: int = 2,
    changed_files: int = 3,
) -> GhPr:
    return GhPr(
        repo="mmb-core",
        number=number,
        title="t",
        body=body,
        state=state,
        url=f"https://github.com/x/r/pull/{number}",
        created_at=created_at,
        merged_at=merged_at,
        head_ref_name=head_ref_name,
        additions=additions,
        deletions=deletions,
        changed_files=changed_files,
    )


def _run(
    db_path: Path,
    mmb_root: Path,
    claude_projects: Path,
    *,
    issues=None,
    prs=None,
    repos=("mmb-core",),
):
    def fi(_owner, repo, **_kw):
        return (issues or {}).get(repo, [])

    def fp(_owner, repo, **_kw):
        return (prs or {}).get(repo, [])

    return reconcile(
        db_path=str(db_path),
        fetch_issues_fn=fi,
        fetch_prs_fn=fp,
        repos=repos,
        andaime_version_fn=lambda: None,
        tooling_root=str(db_path.parent),
        mmb_root=str(mmb_root),
        claude_projects_root=str(claude_projects),
        briefings=[],
        briefings_malformed=[],
        journal_signals=[],
        agent_signals=[],
        now_epoch=0.0,
    )


# ── Unit: derivations puras ───────────────────────────────────────


def test_encode_worktree_path():
    assert (
        encode_worktree_path("/home/eliezer/llab/MMB/mmb-core/.worktrees/X1-foo")
        == "-home-eliezer-llab-MMB-mmb-core--worktrees-X1-foo"
    )


def test_compute_cost_opus():
    """1k input + 1k output em Opus = (1k*15 + 1k*75)/1e6 = $0.09."""
    from mmb_logger.reconcile.transcripts import UsageSums

    s = UsageSums(input_tokens=1000, output_tokens=1000)
    assert compute_cost(s, "claude-opus-4-7") == 0.09


def test_compute_cost_unknown_model():
    from mmb_logger.reconcile.transcripts import UsageSums

    s = UsageSums(input_tokens=1000, output_tokens=1000)
    assert compute_cost(s, "claude-future-99") is None


def test_compute_cost_with_cache():
    """Cache 5m write (1.25x input) + cache read (0.1x input)."""
    from mmb_logger.reconcile.transcripts import UsageSums

    s = UsageSums(
        input_tokens=0,
        output_tokens=0,
        cache_5m_write_tokens=1000,
        cache_read_tokens=1000,
    )
    # Opus: cache_5m_write=18.75 + cache_read=1.50 per MTok
    # (1000 * 18.75 + 1000 * 1.50) / 1e6 = 0.02025
    assert compute_cost(s, "claude-opus-4-7") == 0.02025


def test_dominant_model_picks_most_frequent():
    from mmb_logger.reconcile.transcripts import UsageSums

    s = UsageSums(model_counts={"claude-opus-4-7": 5, "claude-sonnet-4-6": 2})
    assert dominant_model(s) == "claude-opus-4-7"


# ── Integration: transcript único ─────────────────────────────────


def test_transcript_unico_preenche_cost_e_tokens(
    db_path: Path, mmb_root: Path, claude_projects: Path
):
    project_dir = _make_transcript_dir(
        claude_projects, mmb_root, repo="mmb-core", wt_name="X1-foo"
    )
    _write_transcript(
        project_dir,
        turns=[
            _assistant_turn(input_tokens=1000, output_tokens=2000),
            _assistant_turn(input_tokens=500, output_tokens=1000),
        ],
    )
    body = "<!-- mmb-cycle-key: e1/core/2026-05-16T10:00:00Z -->\n# brief"
    _run(
        db_path,
        mmb_root,
        claude_projects,
        issues={"mmb-core": [_issue(body=body)]},
        prs={"mmb-core": [_pr(head_ref_name="task/X1-foo")]},
    )
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT cost_usd, tokens_input, tokens_output FROM ciclos"
        ).fetchone()
        # 1500 input + 3000 output, Opus
        assert row["tokens_input"] == 1500
        assert row["tokens_output"] == 3000
        # (1500*15 + 3000*75)/1e6 = 0.0225 + 0.225 = 0.2475
        assert row["cost_usd"] == 0.2475


def test_transcript_com_cache_tokens(
    db_path: Path, mmb_root: Path, claude_projects: Path
):
    project_dir = _make_transcript_dir(
        claude_projects, mmb_root, repo="mmb-core", wt_name="X1-bar"
    )
    _write_transcript(
        project_dir,
        turns=[
            _assistant_turn(
                input_tokens=100,
                output_tokens=200,
                cache_5m=10000,
                cache_read=20000,
            ),
        ],
    )
    body = "<!-- mmb-cycle-key: e1/core/2026-05-16T10:00:00Z -->"
    _run(
        db_path,
        mmb_root,
        claude_projects,
        issues={"mmb-core": [_issue(body=body)]},
        prs={"mmb-core": [_pr(head_ref_name="task/X1-bar")]},
    )
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT cost_usd, tokens_input, tokens_output FROM ciclos"
        ).fetchone()
        # tokens_input = 100 + 10000 + 20000 = 30100
        assert row["tokens_input"] == 30100
        assert row["tokens_output"] == 200
        # Opus cost:
        # 100*15/1e6 + 200*75/1e6 + 10000*18.75/1e6 + 20000*1.50/1e6
        # = 0.0015 + 0.015 + 0.1875 + 0.03 = 0.234
        assert row["cost_usd"] == 0.234


# ── Transcript ausente ─────────────────────────────────────────────


def test_transcript_ausente_mantem_null_com_warning(
    db_path: Path, mmb_root: Path, claude_projects: Path
):
    """PR existe mas dir de transcript não existe → cost/tokens NULL + warn."""
    body = "<!-- mmb-cycle-key: e1/core/2026-05-16T10:00:00Z -->"
    result = _run(
        db_path,
        mmb_root,
        claude_projects,
        issues={"mmb-core": [_issue(body=body)]},
        prs={"mmb-core": [_pr(head_ref_name="task/NEVER-RAN")]},
    )
    assert any("transcript-missing" in w for w in result.warnings)
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT cost_usd, tokens_input, tokens_output FROM ciclos"
        ).fetchone()
        assert row["cost_usd"] is None
        assert row["tokens_input"] is None
        assert row["tokens_output"] is None


# ── Model desconhecido ────────────────────────────────────────────


def test_model_desconhecido_tokens_si_cost_null(
    db_path: Path, mmb_root: Path, claude_projects: Path
):
    project_dir = _make_transcript_dir(
        claude_projects, mmb_root, repo="mmb-core", wt_name="X1-future"
    )
    _write_transcript(
        project_dir,
        turns=[
            _assistant_turn(
                model="claude-future-99",
                input_tokens=1000,
                output_tokens=2000,
            ),
        ],
    )
    body = "<!-- mmb-cycle-key: e1/core/2026-05-16T10:00:00Z -->"
    result = _run(
        db_path,
        mmb_root,
        claude_projects,
        issues={"mmb-core": [_issue(body=body)]},
        prs={"mmb-core": [_pr(head_ref_name="task/X1-future")]},
    )
    assert any("unknown-model" in w for w in result.warnings)
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT cost_usd, tokens_input, tokens_output FROM ciclos"
        ).fetchone()
        # tokens preenchidos, cost NULL (modelo fora da tabela)
        assert row["tokens_input"] == 1000
        assert row["tokens_output"] == 2000
        assert row["cost_usd"] is None


# ── Multi-session ─────────────────────────────────────────────────


def test_multi_session_soma_com_warning(
    db_path: Path, mmb_root: Path, claude_projects: Path
):
    """2 JSONLs no mesmo dir → soma + warning explícito."""
    project_dir = _make_transcript_dir(
        claude_projects, mmb_root, repo="mmb-core", wt_name="X1-multi"
    )
    _write_transcript(
        project_dir,
        session_uuid="sess-a",
        turns=[_assistant_turn(input_tokens=1000, output_tokens=500)],
    )
    _write_transcript(
        project_dir,
        session_uuid="sess-b",
        turns=[_assistant_turn(input_tokens=2000, output_tokens=1000)],
    )
    body = "<!-- mmb-cycle-key: e1/core/2026-05-16T10:00:00Z -->"
    result = _run(
        db_path,
        mmb_root,
        claude_projects,
        issues={"mmb-core": [_issue(body=body)]},
        prs={"mmb-core": [_pr(head_ref_name="task/X1-multi")]},
    )
    assert any("transcript-multi-session" in w and "2 sess" in w for w in result.warnings)
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT tokens_input, tokens_output FROM ciclos").fetchone()
        assert row["tokens_input"] == 3000  # 1000 + 2000
        assert row["tokens_output"] == 1500  # 500 + 1000


# ── JSONL malformado ──────────────────────────────────────────────


def test_jsonl_malformed_warning_continue(
    db_path: Path, mmb_root: Path, claude_projects: Path
):
    """Linha malformada no meio do JSONL: skip + warning, continua processamento."""
    project_dir = _make_transcript_dir(
        claude_projects, mmb_root, repo="mmb-core", wt_name="X1-broken"
    )
    path = project_dir / "broken-session.jsonl"
    valid_line = json.dumps(
        {"type": "assistant", "message": _assistant_turn(input_tokens=100, output_tokens=200)}
    )
    with path.open("w") as f:
        f.write(valid_line + "\n")
        f.write("THIS IS NOT JSON {{{\n")  # malformed
        f.write(valid_line + "\n")
    body = "<!-- mmb-cycle-key: e1/core/2026-05-16T10:00:00Z -->"
    result = _run(
        db_path,
        mmb_root,
        claude_projects,
        issues={"mmb-core": [_issue(body=body)]},
        prs={"mmb-core": [_pr(head_ref_name="task/X1-broken")]},
    )
    assert any("transcript-malformed-lines" in w for w in result.warnings)
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT tokens_input FROM ciclos").fetchone()
        # 2 valid turns x 100 = 200
        assert row["tokens_input"] == 200


# ── Preservação humana + idempotência ─────────────────────────────


def test_fase4_preserva_campos_humanos(
    db_path: Path, mmb_root: Path, claude_projects: Path
):
    project_dir = _make_transcript_dir(
        claude_projects, mmb_root, repo="mmb-core", wt_name="X1-hum"
    )
    _write_transcript(
        project_dir,
        turns=[_assistant_turn(input_tokens=100, output_tokens=200)],
    )
    body = "<!-- mmb-cycle-key: e1/core/2026-05-16T10:00:00Z -->"
    _run(
        db_path,
        mmb_root,
        claude_projects,
        issues={"mmb-core": [_issue(body=body)]},
        prs={"mmb-core": [_pr(head_ref_name="task/X1-hum")]},
    )
    with get_conn(db_path) as conn:
        cid = conn.execute("SELECT id FROM ciclos").fetchone()["id"]
        patch_ciclo(conn, cid, assertiveness_score=5, review_note="ok")

    _run(
        db_path,
        mmb_root,
        claude_projects,
        issues={"mmb-core": [_issue(body=body)]},
        prs={"mmb-core": [_pr(head_ref_name="task/X1-hum")]},
    )
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT assertiveness_score, review_note, cost_usd FROM ciclos"
        ).fetchone()
        assert row["assertiveness_score"] == 5
        assert row["review_note"] == "ok"
        assert row["cost_usd"] is not None  # custo seguiu computado


def test_fase4_idempotente(
    db_path: Path, mmb_root: Path, claude_projects: Path
):
    project_dir = _make_transcript_dir(
        claude_projects, mmb_root, repo="mmb-core", wt_name="X1-idem"
    )
    _write_transcript(
        project_dir,
        turns=[_assistant_turn(input_tokens=100, output_tokens=200)],
    )
    body = "<!-- mmb-cycle-key: e1/core/2026-05-16T10:00:00Z -->"
    args = dict(
        issues={"mmb-core": [_issue(body=body)]},
        prs={"mmb-core": [_pr(head_ref_name="task/X1-idem")]},
    )
    _run(db_path, mmb_root, claude_projects, **args)
    with get_conn(db_path) as conn:
        snap1 = [dict(r) for r in conn.execute("SELECT * FROM ciclos")]

    _run(db_path, mmb_root, claude_projects, **args)
    with get_conn(db_path) as conn:
        snap2 = [dict(r) for r in conn.execute("SELECT * FROM ciclos")]

    assert snap1 == snap2


# ── Custo volta pra NULL se transcript desaparece ────────────────


def test_cost_volta_null_se_transcript_some(
    db_path: Path, mmb_root: Path, claude_projects: Path
):
    project_dir = _make_transcript_dir(
        claude_projects, mmb_root, repo="mmb-core", wt_name="X1-gone"
    )
    transcript = _write_transcript(
        project_dir,
        turns=[_assistant_turn(input_tokens=100, output_tokens=200)],
    )
    body = "<!-- mmb-cycle-key: e1/core/2026-05-16T10:00:00Z -->"
    _run(
        db_path,
        mmb_root,
        claude_projects,
        issues={"mmb-core": [_issue(body=body)]},
        prs={"mmb-core": [_pr(head_ref_name="task/X1-gone")]},
    )
    with get_conn(db_path) as conn:
        assert conn.execute("SELECT cost_usd FROM ciclos").fetchone()["cost_usd"] is not None

    # Apaga transcript (simula cleanup do usuário)
    transcript.unlink()
    project_dir.rmdir()

    _run(
        db_path,
        mmb_root,
        claude_projects,
        issues={"mmb-core": [_issue(body=body)]},
        prs={"mmb-core": [_pr(head_ref_name="task/X1-gone")]},
    )
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT cost_usd, tokens_input, tokens_output FROM ciclos"
        ).fetchone()
        # Tudo volta pra NULL — sem lixo derivado antigo
        assert row["cost_usd"] is None
        assert row["tokens_input"] is None
        assert row["tokens_output"] is None


# ── Sem PR não tenta computar custo ───────────────────────────────


def test_ciclo_sem_pr_cost_null_sem_warning(
    db_path: Path, mmb_root: Path, claude_projects: Path
):
    """Issue sem PR: cost_usd NULL sem warning transcript-missing."""
    body = "<!-- mmb-cycle-key: e1/core/2026-05-16T10:00:00Z -->"
    result = _run(
        db_path,
        mmb_root,
        claude_projects,
        issues={"mmb-core": [_issue(body=body)]},
        prs={"mmb-core": []},  # sem PRs
    )
    # Nenhum transcript-missing — não tentamos lookup sem PR
    assert not any("transcript-missing" in w for w in result.warnings)
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT cost_usd, tokens_input, tokens_output FROM ciclos"
        ).fetchone()
        assert row["cost_usd"] is None
        assert row["tokens_input"] is None


# ── Tabela de preços coerente ──────────────────────────────────────


def test_pricing_table_tem_modelos_do_config():
    """Models declarados em .tooling/config.sh têm preço documentado."""
    expected = {"claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"}
    assert expected.issubset(set(PRICING.keys()))


def test_pricing_table_cache_ratios_corretos():
    """Sanidade: cache_5m_write = 1.25x input, cache_read = 0.1x input."""
    for model, p in PRICING.items():
        assert p["cache_5m_write"] == pytest.approx(p["input"] * 1.25), model
        assert p["cache_read"] == pytest.approx(p["input"] * 0.10), model
        assert p["cache_1h_write"] == pytest.approx(p["input"] * 2.0), model


# ── find_transcripts edge cases ───────────────────────────────────


def test_find_transcripts_head_ref_fora_da_convencao(
    mmb_root: Path, claude_projects: Path
):
    """head_ref_name que não começa com `task/` devolve lista vazia."""
    out = find_transcripts(
        mmb_root=mmb_root,
        repo="mmb-core",
        head_ref_name="feature/x",
        claude_projects_root=claude_projects,
    )
    assert out == []


def test_find_transcripts_external_target_via_registry(
    tmp_path: Path, claude_projects: Path, monkeypatch
):
    """Target externo: worktree resolvida via `local_path` absoluto do registry.

    Regressão pro bug em que `find_transcripts` compunha worktree sempre
    como `<mmb_root>/<repo>/.worktrees/...`, falhando pra targets com
    `kind=external` (worktree fora de `$MMB_ROOT`).
    """
    from mmb_logger.targets import Target

    external_root = tmp_path / "external" / "campo-premiado"
    (external_root / ".worktrees").mkdir(parents=True)
    fake_target = Target(
        id="campo-premiado",
        dest="campo-premiado",
        repo="campo-premiado",
        local_path=str(external_root),
        worker_profile="project-orchestrator.md",
        agent_layer="project",
        tracked_by_logger=True,
        kind="external",
    )
    monkeypatch.setattr(
        "mmb_logger.reconcile.transcripts.load_targets",
        lambda: [fake_target],
    )

    mmb_root = tmp_path / "MMB"
    mmb_root.mkdir()
    wt_name = "X9-feat"
    worktree_path = str(external_root / ".worktrees" / wt_name)
    encoded = encode_worktree_path(worktree_path)
    project_dir = claude_projects / encoded
    project_dir.mkdir(parents=True)
    jsonl = project_dir / "s1.jsonl"
    jsonl.write_text("{}\n")

    out = find_transcripts(
        mmb_root=mmb_root,
        repo="campo-premiado",
        head_ref_name=f"task/{wt_name}",
        claude_projects_root=claude_projects,
    )
    assert out == [jsonl]


def test_find_transcripts_relative_local_path_resolves_under_mmb_root(
    tmp_path: Path, claude_projects: Path, monkeypatch
):
    """`local_path` relativo: resolvido como `mmb_root / local_path`."""
    from mmb_logger.targets import Target

    fake_target = Target(
        id="cockpit",
        dest="cockpit",
        repo="mmb-cockpit",
        local_path="mmb-cockpit",
        worker_profile="project-orchestrator.md",
        agent_layer="project",
        tracked_by_logger=True,
        kind="internal",
    )
    monkeypatch.setattr(
        "mmb_logger.reconcile.transcripts.load_targets",
        lambda: [fake_target],
    )

    mmb_root = tmp_path / "MMB"
    (mmb_root / "mmb-cockpit" / ".worktrees").mkdir(parents=True)
    wt_name = "Z1-bar"
    worktree_path = str(mmb_root / "mmb-cockpit" / ".worktrees" / wt_name)
    encoded = encode_worktree_path(worktree_path)
    project_dir = claude_projects / encoded
    project_dir.mkdir(parents=True)
    jsonl = project_dir / "s1.jsonl"
    jsonl.write_text("{}\n")

    out = find_transcripts(
        mmb_root=mmb_root,
        repo="mmb-cockpit",
        head_ref_name=f"task/{wt_name}",
        claude_projects_root=claude_projects,
    )
    assert out == [jsonl]


def test_find_transcripts_repo_fora_do_registry_usa_fallback(
    mmb_root: Path, claude_projects: Path, monkeypatch
):
    """Repo ausente do registry cai no fallback `<mmb_root>/<repo>/.worktrees`.

    Backward-compat com fixtures históricas (ex.: `mmb-core`).
    """
    monkeypatch.setattr(
        "mmb_logger.reconcile.transcripts.load_targets",
        lambda: [],
    )

    wt_name = "X1-foo"
    worktree_path = str(mmb_root / "mmb-core" / ".worktrees" / wt_name)
    encoded = encode_worktree_path(worktree_path)
    project_dir = claude_projects / encoded
    project_dir.mkdir(parents=True)
    jsonl = project_dir / "s1.jsonl"
    jsonl.write_text("{}\n")

    out = find_transcripts(
        mmb_root=mmb_root,
        repo="mmb-core",
        head_ref_name=f"task/{wt_name}",
        claude_projects_root=claude_projects,
    )
    assert out == [jsonl]


def test_find_transcripts_load_targets_failure_falls_back(
    mmb_root: Path, claude_projects: Path, monkeypatch
):
    """`load_targets()` levanta: caímos no fallback retro-compat sem crashar."""

    def _boom():
        raise RuntimeError("registry indisponível")

    monkeypatch.setattr(
        "mmb_logger.reconcile.transcripts.load_targets", _boom
    )

    wt_name = "X2-baz"
    worktree_path = str(mmb_root / "mmb-core" / ".worktrees" / wt_name)
    encoded = encode_worktree_path(worktree_path)
    project_dir = claude_projects / encoded
    project_dir.mkdir(parents=True)
    jsonl = project_dir / "s1.jsonl"
    jsonl.write_text("{}\n")

    out = find_transcripts(
        mmb_root=mmb_root,
        repo="mmb-core",
        head_ref_name=f"task/{wt_name}",
        claude_projects_root=claude_projects,
    )
    assert out == [jsonl]


def test_sum_usage_from_transcript_empty_file(tmp_path: Path):
    """Arquivo vazio: nenhum turn lido, malformed_lines = 0."""
    f = tmp_path / "empty.jsonl"
    f.write_text("")
    s = sum_usage_from_transcript(f)
    assert s.valid_turns == 0
    assert s.input_tokens == 0
