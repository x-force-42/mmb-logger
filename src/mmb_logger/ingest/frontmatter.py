"""Parser de frontmatter YAML simples.

Aceita formato:
---
key: value
key2: value2
---

Body em markdown vem depois. Sem aninhamento — chave: valor por linha.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


@dataclass
class ParsedFile:
    frontmatter: dict[str, str]
    body: str


def parse(text: str) -> ParsedFile:
    """Parseia texto com frontmatter.

    Linhas no bloco frontmatter assumem formato `key: value`. Valores
    com `:` no meio (URLs) são preservados — particionamos só no primeiro `:`.
    Aspas simples ou duplas em volta do valor são removidas se cercam tudo.
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        return ParsedFile(frontmatter={}, body=text)
    fm_block = m.group(1)
    body = m.group(2)
    fm: dict[str, str] = {}
    for line in fm_block.split("\n"):
        stripped = line.strip()
        if not stripped or ":" not in stripped:
            continue
        k, _, v = stripped.partition(":")
        key = k.strip()
        value = v.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        fm[key] = value
    return ParsedFile(frontmatter=fm, body=body)
