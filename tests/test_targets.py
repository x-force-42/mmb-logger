"""Testes do cliente Python do registry de targets.

Cobre PR 1A schema + PR 2A opcional fields.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mmb_logger.targets import (
    RegistryError,
    historical_dest_ids,
    load_targets,
    repos_tracked,
    target_ids,
)


def _write_registry(tmp_path: Path, targets: list[dict]) -> Path:
    path = tmp_path / "targets.json"
    path.write_text(json.dumps({"schema_version": 1, "targets": targets}))
    return path


def _internal_target(id: str, **overrides) -> dict:
    base = {
        "id": id,
        "dest": id,
        "repo": f"mmb-{id}",
        "local_path": f"mmb-{id}",
        "worker_profile": "project-orchestrator.md",
        "agent_layer": "project",
        "tracked_by_logger": True,
    }
    base.update(overrides)
    return base


def test_load_minimal_schema_with_defaults(tmp_path: Path) -> None:
    """Schema mínimo (sem campos opcionais) carrega aplicando defaults."""
    p = _write_registry(tmp_path, [_internal_target("cockpit")])
    targets = load_targets(p)
    assert len(targets) == 1
    t = targets[0]
    assert t.owner is None
    assert t.requires_github is True
    assert t.kind == "internal"
    assert t.managed_by_reset is True


def test_load_with_explicit_optional_fields(tmp_path: Path) -> None:
    p = _write_registry(
        tmp_path,
        [
            _internal_target(
                "cockpit",
                owner="x-force-42",
                requires_github=True,
                kind="internal",
                managed_by_reset=True,
            )
        ],
    )
    t = load_targets(p)[0]
    assert t.owner == "x-force-42"
    assert t.kind == "internal"


def test_load_external_fake_with_absolute_path(tmp_path: Path) -> None:
    abs_path = str(tmp_path / "external" / "weather-cli")
    p = _write_registry(
        tmp_path,
        [
            {
                "id": "weather-cli",
                "dest": "weather-cli",
                "repo": "weather-cli",
                "local_path": abs_path,
                "worker_profile": "project-orchestrator.md",
                "agent_layer": "project",
                "tracked_by_logger": False,
                "owner": "",
                "requires_github": False,
                "kind": "external-fake",
                "managed_by_reset": False,
            }
        ],
    )
    t = load_targets(p)[0]
    assert t.local_path == abs_path  # parser não toca; consumidor decide
    assert t.kind == "external-fake"
    assert t.requires_github is False
    assert t.managed_by_reset is False
    assert t.tracked_by_logger is False
    assert t.owner == ""  # string vazia preservada (caller usa MMB_GH_OWNER)


def test_invalid_kind_rejected(tmp_path: Path) -> None:
    p = _write_registry(
        tmp_path, [_internal_target("cockpit", kind="not-valid")]
    )
    with pytest.raises(RegistryError, match="kind inválido"):
        load_targets(p)


def test_invalid_owner_type_rejected(tmp_path: Path) -> None:
    p = _write_registry(
        tmp_path, [_internal_target("cockpit", owner=123)]
    )
    with pytest.raises(RegistryError, match="owner não é string ou null"):
        load_targets(p)


def test_owner_null_accepted(tmp_path: Path) -> None:
    p = _write_registry(tmp_path, [_internal_target("cockpit", owner=None)])
    t = load_targets(p)[0]
    assert t.owner is None


def test_extra_unknown_field_rejected(tmp_path: Path) -> None:
    p = _write_registry(
        tmp_path, [_internal_target("cockpit", surprise="nope")]
    )
    with pytest.raises(RegistryError, match="campos extras"):
        load_targets(p)


def test_requires_github_non_bool_rejected(tmp_path: Path) -> None:
    p = _write_registry(
        tmp_path, [_internal_target("cockpit", requires_github="true")]
    )
    with pytest.raises(RegistryError, match="requires_github não é booleano"):
        load_targets(p)


def test_managed_by_reset_non_bool_rejected(tmp_path: Path) -> None:
    p = _write_registry(
        tmp_path, [_internal_target("cockpit", managed_by_reset=1)]
    )
    with pytest.raises(RegistryError, match="managed_by_reset não é booleano"):
        load_targets(p)


def test_repos_tracked_uses_default_true(tmp_path: Path, monkeypatch) -> None:
    """repos_tracked respeita tracked_by_logger sem alterações de PR 2A."""
    p = _write_registry(
        tmp_path,
        [
            _internal_target("cockpit", tracked_by_logger=True),
            _internal_target("ext", tracked_by_logger=False, kind="external"),
        ],
    )
    # Força reload deste path explicitamente.
    monkeypatch.setenv("MMB_TARGETS_FILE", str(p))
    # Limpa cache module-level.
    import mmb_logger.targets as mod
    mod._cache = None
    mod._cache_path = None
    assert "mmb-cockpit" in repos_tracked()
    assert "mmb-ext" not in repos_tracked()
    assert "cockpit" in target_ids()
    assert "ext" in target_ids()
    assert "core" in historical_dest_ids()
