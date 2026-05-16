"""Testes do parser de agents stream."""

from __future__ import annotations

from pathlib import Path

from mmb_logger.ingest.agents_stream import decode_agent_id, parse_line, read_agents_after


def test_parse_spawn():
    e = parse_line('{"ts":"t","ev":"spawn","id":"cockpit-M2","pid":1234,"task":"M2","epic":"ep1"}')
    assert e is not None
    assert e.ev == "spawn"
    assert e.pid == 1234
    assert e.task == "M2"


def test_parse_deregister_com_reason():
    e = parse_line('{"ts":"t","ev":"deregister","id":"cockpit-M2","reason":"heartbeat-timeout"}')
    assert e is not None
    assert e.reason == "heartbeat-timeout"


def test_decode_agent_id():
    assert decode_agent_id("cockpit-M2") == ("cockpit", "M2")
    assert decode_agent_id("core-X1") == ("core", "X1")
    assert decode_agent_id("master") == (None, None)
    assert decode_agent_id("commd-pid12345") == (None, None)


def test_read_agents_after(tmp_path: Path):
    p = tmp_path / "a.jsonl"
    p.write_text(
        '{"ts":"t1","ev":"spawn","id":"cockpit-M2","pid":1}\n'
        '{"ts":"t2","ev":"heartbeat","id":"cockpit-M2"}\n'
        '{"ts":"t3","ev":"deregister","id":"cockpit-M2","reason":"manual"}\n'
    )
    eventos = list(read_agents_after(p, 0))
    assert len(eventos) == 3
    assert [e[0].ev for e in eventos] == ["spawn", "heartbeat", "deregister"]
