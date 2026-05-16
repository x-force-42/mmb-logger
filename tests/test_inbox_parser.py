"""Testes do parser de inbox."""

from __future__ import annotations

from pathlib import Path

from mmb_logger.ingest.inbox import (
    is_lifecycle_path,
    iter_inbox_files,
    iter_live_inbox_files,
    parse_inbox_file,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_briefing():
    msg = parse_inbox_file(FIXTURES / "inbox_briefing.md")
    assert msg is not None
    assert msg.from_ == "master"
    assert msg.to == "cockpit"
    assert msg.type == "briefing"
    assert msg.thread == "mmb-logger-destilacao"
    assert "M2 wire-up" in msg.body


def test_parse_arquivo_sem_campos(tmp_path: Path):
    p = tmp_path / "invalido.md"
    p.write_text("apenas corpo, sem frontmatter")
    assert parse_inbox_file(p) is None


def test_iter_inbox_files_inclui_lifecycle(tmp_path: Path):
    base = tmp_path / "inbox" / "master"
    base.mkdir(parents=True)
    (base / "msg.md").write_text("---\nfrom: a\nto: b\ntype: c\nsubject: d\ncreated: e\n---\n")
    done = base / ".done"
    done.mkdir()
    (done / "antigo.md").write_text("---\nfrom: a\nto: b\ntype: c\nsubject: d\ncreated: e\n---\n")
    paths = iter_inbox_files(tmp_path)
    assert len(paths) == 2


def test_iter_live_inbox_files_exclui_lifecycle(tmp_path: Path):
    base = tmp_path / "inbox" / "master"
    base.mkdir(parents=True)
    (base / "vivo.md").write_text("x")
    done = base / ".done"
    done.mkdir()
    (done / "antigo.md").write_text("x")
    paths = iter_live_inbox_files(tmp_path)
    assert len(paths) == 1
    assert paths[0].name == "vivo.md"


def test_is_lifecycle_path():
    assert is_lifecycle_path("/foo/inbox/master/.done/x.md")
    assert is_lifecycle_path("/foo/inbox/.processing/x.md")
    assert is_lifecycle_path("/foo/inbox/master/.dead/x.md")
    assert not is_lifecycle_path("/foo/inbox/master/x.md")
