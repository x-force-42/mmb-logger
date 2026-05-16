"""Testes do parser de frontmatter."""

from __future__ import annotations

from mmb_logger.ingest.frontmatter import parse


def test_sem_frontmatter():
    p = parse("apenas corpo markdown\nlinha 2")
    assert p.frontmatter == {}
    assert p.body == "apenas corpo markdown\nlinha 2"


def test_frontmatter_completo():
    text = "---\nfrom: master\nto: cockpit\ntype: briefing\n---\ncorpo aqui"
    p = parse(text)
    assert p.frontmatter == {"from": "master", "to": "cockpit", "type": "briefing"}
    assert p.body == "corpo aqui"


def test_frontmatter_body_vazio():
    text = "---\nfrom: a\nto: b\n---\n"
    p = parse(text)
    assert p.frontmatter == {"from": "a", "to": "b"}
    assert p.body == ""


def test_frontmatter_body_multilinha():
    text = "---\nfrom: a\n---\nl1\nl2\nl3"
    p = parse(text)
    assert p.body == "l1\nl2\nl3"


def test_frontmatter_url_com_doispontos():
    text = "---\nurl: https://github.com/x-force-42/mmb-core/pull/12\nfrom: a\n---\n"
    p = parse(text)
    assert p.frontmatter["url"] == "https://github.com/x-force-42/mmb-core/pull/12"


def test_frontmatter_aspas_removidas():
    text = "---\nsubject: 'issue-criada-42'\n---\n"
    p = parse(text)
    assert p.frontmatter["subject"] == "issue-criada-42"
