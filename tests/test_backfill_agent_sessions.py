"""Testes pro backfill de agent_sessions.

Cobre: classificação de role, normalização de task_id, parsing de transcript,
custo, linkagem por confiança, idempotência do UPSERT, persistência de ORPHAN.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mmb_logger.backfill.agent_sessions import (
    SessionRecord,
    backfill_agent_sessions,
    classify_dir,
    compute_cost,
    extract_cycle_key,
    extract_task_id_raw,
    link_via_github,
    normalize_task_id,
    parse_session_metrics,
    upsert_session,
)

# ── Classificação de role ─────────────────────────────────────────────────


def test_classify_role_master() -> None:
    role, repo, slug = classify_dir(
        "-home-eliezer-llab-MMB", Path("/home/eliezer/llab/MMB")
    )
    assert role == "master"
    assert repo is None
    assert slug is None


def test_classify_role_atomic() -> None:
    role, repo, slug = classify_dir(
        "-home-eliezer-llab-MMB-mmb-cockpit--worktrees-M7-cockpit-ui-fixes",
        Path("/home/eliezer/llab/MMB"),
    )
    assert role == "atomic"
    assert repo == "cockpit"
    assert slug == "M7-cockpit-ui-fixes"


def test_classify_role_manual() -> None:
    role, repo, slug = classify_dir(
        "-home-eliezer-llab-MMB-mmb-logger", Path("/home/eliezer/llab/MMB")
    )
    assert role == "manual"
    assert repo == "logger"
    assert slug is None


def test_classify_role_unknown_outside_mmb() -> None:
    role, repo, slug = classify_dir(
        "-home-eliezer-some-other-project", Path("/home/eliezer/llab/MMB")
    )
    assert role == "unknown"
    assert repo is None
    assert slug is None


# ── Extração e normalização de task_id ────────────────────────────────────


def test_extract_task_id_raw_letter_digit() -> None:
    assert extract_task_id_raw("M7-cockpit-ui-fixes") == "M7"
    assert extract_task_id_raw("A1-role-planner") == "A1"
    assert extract_task_id_raw("X1-cleanup-task-scripts") == "X1"


def test_extract_task_id_raw_dotted_numeric_encoded() -> None:
    # encoding troca '.' por '-': '1.1' → '1-1' no nome do dir
    assert extract_task_id_raw("1-1-blocos-progresso") == "1-1"
    assert extract_task_id_raw("2-1-smoke-visual-e") == "2-1"


def test_extract_task_id_raw_plain_numeric() -> None:
    assert extract_task_id_raw("37-human-intent-instruction") == "37"


def test_extract_task_id_raw_no_match() -> None:
    assert extract_task_id_raw(None) is None
    assert extract_task_id_raw("") is None
    assert extract_task_id_raw("sem-prefixo-reconhecido") is None


def test_normalize_task_id_promotes_dash_to_dot_via_branch() -> None:
    normalized, rule = normalize_task_id("1-1", "task/1.1-blocos-progresso")
    assert normalized == "1.1"
    assert "promovido" in rule
    assert "1-1" in rule  # preserva raw no histórico


def test_normalize_task_id_preserves_raw_when_branch_matches() -> None:
    normalized, rule = normalize_task_id("M7", "task/M7-cockpit-ui-fixes")
    assert normalized == "M7"
    assert "casa" in rule.lower() or "raw" in rule


def test_normalize_task_id_without_branch() -> None:
    normalized, _rule = normalize_task_id("M7", None)
    assert normalized == "M7"
    normalized, _rule = normalize_task_id("M7", "")
    assert normalized == "M7"


def test_normalize_task_id_raw_none() -> None:
    normalized, rule = normalize_task_id(None, "task/A1-foo")
    assert normalized == "A1"
    assert "raw=None" in rule or "raw" in rule


# ── Parsing de transcript ─────────────────────────────────────────────────


def _write_transcript(path: Path, lines: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for d in lines:
            f.write(json.dumps(d) + "\n")
    return path


def test_parse_session_metrics_minimal_transcript(tmp_path: Path) -> None:
    p = _write_transcript(
        tmp_path / "sess-abc.jsonl",
        [
            {"type": "permission-mode", "permissionMode": "bypassPermissions"},
            {
                "type": "user",
                "timestamp": "2026-05-19T10:00:00.000Z",
                "cwd": "/home/eliezer/llab/MMB/mmb-cockpit/.worktrees/M7-cockpit-ui-fixes",
                "gitBranch": "task/M7-cockpit-ui-fixes",
                "version": "1.0.0",
            },
            {
                "type": "assistant",
                "timestamp": "2026-05-19T10:00:05.000Z",
                "message": {
                    "model": "claude-opus-4-7",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 200,
                        "cache_read_input_tokens": 50,
                        "cache_creation": {"ephemeral_5m_input_tokens": 30},
                    },
                    "stop_reason": "tool_use",
                    "content": [
                        {"type": "tool_use", "name": "Bash",
                         "input": {"command": "ls"}},
                        {"type": "tool_use", "name": "Read",
                         "input": {"file_path": "/tmp/foo.txt"}},
                    ],
                },
            },
            {
                "type": "assistant",
                "timestamp": "2026-05-19T10:00:10.000Z",
                "message": {
                    "model": "claude-opus-4-7",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                    "content": [
                        {"type": "tool_use", "name": "Edit",
                         "input": {"file_path": "/tmp/bar.py"}},
                    ],
                },
            },
        ],
    )
    rec = parse_session_metrics(p)
    assert rec.session_id == "sess-abc"
    assert rec.num_turns == 3
    assert rec.input_tokens == 110
    assert rec.output_tokens == 205
    assert rec.cache_read_tokens == 50
    assert rec.cache_creation_tokens == 30
    assert rec.model_resolved == "claude-opus-4-7"
    assert rec.permission_mode == "bypassPermissions"
    assert rec.claude_version == "1.0.0"
    assert rec.tool_calls_by_name == {"Bash": 1, "Read": 1, "Edit": 1}
    assert "/tmp/foo.txt" in rec.files_read
    assert "/tmp/bar.py" in rec.files_edited
    assert rec.bash_commands_count == 1
    assert rec.duration_wall_ms == 10000  # 10s
    assert rec.active_interaction_ms is not None
    assert rec.has_synthetic_turns is False


def test_parse_session_metrics_detects_synthetic(tmp_path: Path) -> None:
    p = _write_transcript(
        tmp_path / "synth.jsonl",
        [
            {
                "type": "assistant",
                "timestamp": "2026-05-19T10:00:00.000Z",
                "message": {
                    "model": "claude-opus-4-7",
                    "usage": {"input_tokens": 100, "output_tokens": 200},
                },
            },
            {
                "type": "assistant",
                "timestamp": "2026-05-19T10:00:01.000Z",
                "message": {
                    "model": "<synthetic>",
                    "usage": {"input_tokens": 5, "output_tokens": 10},
                },
            },
        ],
    )
    rec = parse_session_metrics(p)
    assert rec.has_synthetic_turns is True
    assert rec.model_resolved == "claude-opus-4-7"  # dominante (mais turns? empate aqui)


def test_parse_session_metrics_skips_malformed(tmp_path: Path) -> None:
    p = tmp_path / "broken.jsonl"
    p.write_text(
        '{"type":"user","timestamp":"2026-05-19T10:00:00Z"}\n'
        "isso não é json\n"
        '{"type":"assistant","timestamp":"2026-05-19T10:00:01Z",'
        '"message":{"model":"claude-opus-4-7","usage":{"input_tokens":1,"output_tokens":1}}}\n',
        encoding="utf-8",
    )
    rec = parse_session_metrics(p)
    assert rec._malformed_lines == 1
    assert rec.num_turns == 2  # user + assistant válidos


# ── Custo ─────────────────────────────────────────────────────────────────


def test_compute_cost_known_model() -> None:
    rec = SessionRecord(session_id="s", transcript_path="/tmp/s.jsonl")
    rec.model_resolved = "claude-opus-4-7"
    rec.models = {"claude-opus-4-7": 10}
    rec.input_tokens = 1_000_000  # 1M input @ $15 = $15
    rec.output_tokens = 0
    cost, conf = compute_cost(rec)
    assert cost == 15.0
    assert conf == "ok_unvalidated"


def test_compute_cost_unknown_model_returns_null() -> None:
    rec = SessionRecord(session_id="s", transcript_path="/tmp/s.jsonl")
    rec.model_resolved = "claude-unicorn-9-9"
    rec.models = {"claude-unicorn-9-9": 5}
    cost, conf = compute_cost(rec)
    assert cost is None
    assert conf == "unknown_model"


def test_compute_cost_no_model() -> None:
    rec = SessionRecord(session_id="s", transcript_path="/tmp/s.jsonl")
    cost, conf = compute_cost(rec)
    assert cost is None
    assert conf == "no_model"


def test_compute_cost_has_synthetic_flag() -> None:
    rec = SessionRecord(session_id="s", transcript_path="/tmp/s.jsonl")
    rec.model_resolved = "claude-opus-4-7"
    rec.models = {"claude-opus-4-7": 5}
    rec.input_tokens = 1000
    rec.has_synthetic_turns = True
    cost, conf = compute_cost(rec)
    assert cost is not None  # tokens são reais; só sinaliza
    assert conf == "has_synthetic"


def test_compute_cost_mixed_models() -> None:
    rec = SessionRecord(session_id="s", transcript_path="/tmp/s.jsonl")
    rec.model_resolved = "claude-opus-4-7"
    rec.models = {"claude-opus-4-7": 5, "claude-sonnet-4-6": 3}
    rec.input_tokens = 1000
    cost, conf = compute_cost(rec)
    assert cost is not None
    assert conf == "mixed_models"


# ── Linkagem GH (sem rede; fixtures inline) ───────────────────────────────


def test_extract_cycle_key() -> None:
    body = (
        "<!-- mmb-cycle-key: agent-sessions/logger/2026-05-20T17:00:00Z\n"
        "     mmb-briefing-file: foo.md -->\n\n# Issue Title\n"
    )
    assert (
        extract_cycle_key(body)
        == "agent-sessions/logger/2026-05-20T17:00:00Z"
    )


def test_extract_cycle_key_absent() -> None:
    assert extract_cycle_key("# normal body") is None
    assert extract_cycle_key(None) is None
    assert extract_cycle_key("") is None


def test_link_via_github_orphan_for_master() -> None:
    rec = SessionRecord(session_id="s", transcript_path="/tmp/s.jsonl")
    rec.role = "master"
    link_via_github(rec, prs_by_target={}, issues_cache={}, owner_by_repo={})
    assert rec.link_confidence == "ORPHAN"
    assert "estado válido" in (rec.link_reason or "")


def test_link_via_github_high_with_anchor() -> None:
    rec = SessionRecord(session_id="s", transcript_path="/tmp/s.jsonl")
    rec.role = "atomic"
    rec.project = "cockpit"
    rec.git_branch = "task/M7-cockpit-ui-fixes"
    prs = {
        "mmb-cockpit": {
            "task/M7-cockpit-ui-fixes": {
                "number": 26,
                "state": "MERGED",
                "mergedAt": "2026-05-18T23:15:32Z",
                "url": "https://github.com/x-force-42/mmb-cockpit/pull/26",
                "closingIssuesReferences": [{"number": 25}],
            }
        }
    }
    issues_cache = {
        ("mmb-cockpit", 25): {
            "body": "<!-- mmb-cycle-key: cockpit-ui-fixes/cockpit/2026-05-18T17:40:34Z -->",
            "labels": [],
        }
    }
    link_via_github(rec, prs_by_target=prs, issues_cache=issues_cache,
                     owner_by_repo={"mmb-cockpit": "x-force-42"})
    assert rec.link_confidence == "HIGH"
    assert rec.candidate_pr_number == 26
    assert rec.candidate_issue_number == 25
    assert rec.evidence["mmb_cycle_key"].startswith("cockpit-ui-fixes/")


def test_link_via_github_medium_without_anchor() -> None:
    rec = SessionRecord(session_id="s", transcript_path="/tmp/s.jsonl")
    rec.role = "atomic"
    rec.project = "cockpit"
    rec.git_branch = "task/M7-cockpit-ui-fixes"
    prs = {
        "mmb-cockpit": {
            "task/M7-cockpit-ui-fixes": {
                "number": 26,
                "state": "MERGED",
                "url": "https://github.com/x-force-42/mmb-cockpit/pull/26",
                "closingIssuesReferences": [{"number": 25}],
            }
        }
    }
    issues_cache = {("mmb-cockpit", 25): {"body": "# no anchor here", "labels": []}}
    link_via_github(rec, prs_by_target=prs, issues_cache=issues_cache,
                     owner_by_repo={"mmb-cockpit": "x-force-42"})
    assert rec.link_confidence == "MEDIUM"


def test_link_via_github_low_without_pr() -> None:
    rec = SessionRecord(session_id="s", transcript_path="/tmp/s.jsonl")
    rec.role = "atomic"
    rec.project = "aquarium"
    rec.git_branch = "task/2.1-smoke-visual-e"
    prs = {"mmb-aquarium": {}}  # nenhum PR casa
    link_via_github(rec, prs_by_target=prs, issues_cache={},
                     owner_by_repo={"mmb-aquarium": "x-force-42"})
    assert rec.link_confidence == "LOW"


# ── UPSERT idempotente ────────────────────────────────────────────────────


def _minimal_record(session_id: str = "sess-1", *, role: str = "atomic",
                     ciclo_id: str | None = None) -> SessionRecord:
    rec = SessionRecord(
        session_id=session_id,
        transcript_path=f"/tmp/{session_id}.jsonl",
        role=role,
        link_confidence="HIGH" if role == "atomic" else "ORPHAN",
        link_reason="test",
        ciclo_id=ciclo_id,
        project="cockpit",
        task_id_raw="M7",
        task_id_normalized="M7",
        slug="M7-cockpit-ui-fixes",
        started_at="2026-05-19T10:00:00Z",
        ended_at="2026-05-19T10:05:00Z",
        duration_wall_ms=300_000,
        active_interaction_ms=120_000,
        num_turns=10,
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=2000,
        cache_creation_tokens=100,
        model_resolved="claude-opus-4-7",
        cost_usd_estimated=0.5,
        cost_pricing_version="2026-05-16",
        cost_confidence="ok_unvalidated",
    )
    return rec


def test_upsert_inserts_new_session(conn) -> None:
    rec = _minimal_record("sess-new")
    outcome = upsert_session(conn, rec)
    assert outcome == "inserted"
    row = conn.execute(
        "SELECT role, link_confidence, cost_usd_estimated FROM agent_sessions WHERE session_id = ?",
        ("sess-new",),
    ).fetchone()
    assert row is not None
    assert row["role"] == "atomic"
    assert row["link_confidence"] == "HIGH"


def test_upsert_idempotent_no_duplicate(conn) -> None:
    rec = _minimal_record("sess-idem")
    upsert_session(conn, rec)
    outcome = upsert_session(conn, rec)
    assert outcome == "updated"
    n = conn.execute(
        "SELECT COUNT(*) FROM agent_sessions WHERE session_id = ?", ("sess-idem",)
    ).fetchone()[0]
    assert n == 1


def test_upsert_updates_derived_fields(conn) -> None:
    rec = _minimal_record("sess-upd")
    upsert_session(conn, rec)
    rec.cost_usd_estimated = 1.5
    rec.link_confidence = "MEDIUM"
    rec.link_reason = "novo motivo"
    upsert_session(conn, rec)
    row = conn.execute(
        "SELECT cost_usd_estimated, link_confidence, link_reason "
        "FROM agent_sessions WHERE session_id = ?",
        ("sess-upd",),
    ).fetchone()
    assert row["cost_usd_estimated"] == 1.5
    assert row["link_confidence"] == "MEDIUM"
    assert row["link_reason"] == "novo motivo"


def test_orphan_persisted_with_null_ciclo_id(conn) -> None:
    rec = SessionRecord(
        session_id="sess-master",
        transcript_path="/tmp/sess-master.jsonl",
        role="master",
        link_confidence="ORPHAN",
        link_reason="role=master",
        started_at="2026-05-19T10:00:00Z",
        ended_at="2026-05-19T12:00:00Z",
        duration_wall_ms=7_200_000,
        model_resolved="claude-opus-4-7",
        cost_usd_estimated=2.5,
        cost_pricing_version="2026-05-16",
        cost_confidence="ok_unvalidated",
    )
    upsert_session(conn, rec)
    row = conn.execute(
        "SELECT ciclo_id, role, link_confidence FROM agent_sessions WHERE session_id = ?",
        ("sess-master",),
    ).fetchone()
    assert row is not None
    assert row["ciclo_id"] is None
    assert row["role"] == "master"
    assert row["link_confidence"] == "ORPHAN"


# ── Fail-safe da API pública ──────────────────────────────────────────────


def test_failsafe_no_flag_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Especifique --dry-run"):
        backfill_agent_sessions(
            db_path=str(tmp_path / "x.db"),
            mmb_root=str(tmp_path),
            write=False,
            dry_run=False,
        )


def test_failsafe_both_flags_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="mutuamente exclusivos"):
        backfill_agent_sessions(
            db_path=str(tmp_path / "x.db"),
            mmb_root=str(tmp_path),
            write=True,
            dry_run=True,
        )


# ── DRY-RUN end-to-end com fixture mínima ────────────────────────────────


def test_dry_run_end_to_end_minimal(tmp_path: Path) -> None:
    """Cria estrutura mínima de transcripts + .tooling/ e roda dry-run."""
    mmb_root = tmp_path / "MMB"
    (mmb_root / ".tooling").mkdir(parents=True)
    # targets.json mínimo (apenas pra load_targets não falhar)
    (mmb_root / ".tooling" / "targets.json").write_text(json.dumps({
        "schema_version": 1,
        "targets": [
            {
                "id": "cockpit", "dest": "cockpit", "repo": "mmb-cockpit",
                "local_path": "mmb-cockpit", "worker_profile": "p.md",
                "agent_layer": "project", "tracked_by_logger": True,
                "owner": "x-force-42",
            }
        ]
    }))

    # Transcript de master + atomic
    claude_projects = tmp_path / "claude-projects"
    master_dir = claude_projects / "-tmp-x-MMB"  # encoding determinístico
    # Vamos usar o encoding real:
    from mmb_logger.reconcile.transcripts import encode_worktree_path
    enc_master = encode_worktree_path(str(mmb_root))
    master_dir = claude_projects / enc_master
    _write_transcript(
        master_dir / "sess-master.jsonl",
        [
            {
                "type": "assistant",
                "timestamp": "2026-05-19T10:00:00.000Z",
                "message": {
                    "model": "claude-opus-4-7",
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                },
            }
        ],
    )

    result = backfill_agent_sessions(
        mmb_root=str(mmb_root),
        claude_projects=str(claude_projects),
        dry_run=True,
        no_gh=True,  # offline pra testes
    )
    assert result.sessions_processed == 1
    assert result.by_role.get("master") == 1
    # ORPHAN porque role=master
    assert result.by_confidence.get("ORPHAN") == 1
    # cost > 0 (model conhecido)
    assert result.cost_total_usd > 0
