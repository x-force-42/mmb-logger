"""Testes do parser de journal."""

from __future__ import annotations

from pathlib import Path

from mmb_logger.ingest.journal import parse_line, read_journal_after


def test_parse_line_valida():
    e = parse_line('{"ts":"2026-01-01T00:00:00Z","sev":"warn","ev":"x","msg":"m"}')
    assert e is not None
    assert e.sev == "warn"
    assert e.ev == "x"
    assert e.msg == "m"


def test_parse_line_invalida():
    assert parse_line("não é json") is None
    assert parse_line('{"falta":"campos"}') is None
    assert parse_line("") is None


def test_read_journal_after(tmp_path: Path):
    p = tmp_path / "j.jsonl"
    p.write_text(
        '{"ts":"t1","sev":"warn","ev":"a","msg":""}\n{"ts":"t2","sev":"error","ev":"b","msg":""}\n'
    )
    seen = list(read_journal_after(p, 0))
    assert len(seen) == 2
    assert seen[0][0].ev == "a"
    last_offset = seen[-1][1]
    # Após offset, nada mais.
    assert list(read_journal_after(p, last_offset)) == []
