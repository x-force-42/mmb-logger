"""Cost + token aggregation a partir de transcripts Claude.

Localiza transcripts via encoding determinístico do worktree path,
soma `usage` por turn, identifica modelo e aplica tabela de preços
documentada. NUNCA estima custo — quando faltam dados, devolve NULL
honesto + warning.

Estrutura real do transcript (validada em FS):

  ~/.claude/projects/<encoded-worktree-path>/<session-uuid>.jsonl

  Cada linha JSONL tem nível superior `{parentUuid, message, ...}`.
  Turns com usage estão em `message.type == "message"` com:
    message.model: str
    message.usage: {
      input_tokens, output_tokens,
      cache_creation_input_tokens, cache_read_input_tokens,
      cache_creation: {
        ephemeral_5m_input_tokens, ephemeral_1h_input_tokens
      }
    }

Encoding: `path.replace("/", "-").replace(".", "-")` — validado fase 0.

Tabela de preços (per million tokens, USD) — última verificação 2026-05-16:
fonte = anthropic.com/pricing. Atualizar quando muda preço público.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from mmb_logger.targets import load_targets

# ── Preços per million tokens, USD ────────────────────────────────
#
# Cache 5m: 1.25x input rate
# Cache 1h: 2x input rate
# Cache read: 0.1x input rate
#
# Manter sincronizado com source-of-truth.md "Tabela de preços".

PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-7": {
        "input": 15.0,
        "output": 75.0,
        "cache_5m_write": 18.75,
        "cache_1h_write": 30.0,
        "cache_read": 1.50,
    },
    "claude-sonnet-4-6": {
        "input": 3.0,
        "output": 15.0,
        "cache_5m_write": 3.75,
        "cache_1h_write": 6.0,
        "cache_read": 0.30,
    },
    "claude-haiku-4-5-20251001": {
        "input": 1.0,
        "output": 5.0,
        "cache_5m_write": 1.25,
        "cache_1h_write": 2.0,
        "cache_read": 0.10,
    },
}


@dataclass
class UsageSums:
    """Soma de usage por categoria de billable token."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_5m_write_tokens: int = 0
    cache_1h_write_tokens: int = 0
    cache_read_tokens: int = 0
    # Distribuição de model — usado pra detectar mistura
    model_counts: dict[str, int] = field(default_factory=dict)
    malformed_lines: int = 0
    valid_turns: int = 0


@dataclass(frozen=True)
class CostResult:
    """Resultado de agregação por ciclo, pronto pra UPSERT."""

    tokens_input: int
    tokens_output: int
    cost_usd: float | None  # None quando modelo desconhecido
    model: str | None
    sessions_count: int


# ── Localização ────────────────────────────────────────────────────


def encode_worktree_path(abs_path: str) -> str:
    """Convenção do Claude Code: substitui `/` e `.` por `-`.

    Validado fase 0 com transcripts reais do MMB:
      /home/eliezer/llab/MMB/mmb-cockpit/.worktrees/X1-cleanup-task-scripts
      → -home-eliezer-llab-MMB-mmb-cockpit--worktrees-X1-cleanup-task-scripts
    """
    return abs_path.replace("/", "-").replace(".", "-")


def find_transcripts(
    *,
    mmb_root: Path,
    repo: str,
    head_ref_name: str,
    claude_projects_root: Path,
) -> list[Path]:
    """Devolve JSONLs da worktree esperada para o PR.

    head_ref_name precisa começar com `task/` (convenção atomic). Worktree
    path: `<base>/.worktrees/<id>-<slug>`, onde `<base>` é o `local_path`
    do target no registry (resolvido contra `mmb_root` se relativo). Se o
    repo não está no registry, fallback pra `<mmb_root>/<repo>/.worktrees`
    — preserva backward-compat com fixtures históricas (ex: `mmb-core`).
    """
    if not head_ref_name.startswith("task/"):
        return []
    wt_name = head_ref_name[len("task/") :]
    try:
        targets = load_targets()
    except Exception:
        targets = []
    base: Path | None = None
    for t in targets:
        if t.repo == repo:
            lp = Path(t.local_path)
            base = lp if lp.is_absolute() else Path(mmb_root) / lp
            break
    if base is None:
        base = Path(mmb_root) / repo
    worktree_path = str(base / ".worktrees" / wt_name)
    encoded = encode_worktree_path(worktree_path)
    project_dir = Path(claude_projects_root) / encoded
    if not project_dir.is_dir():
        return []
    return sorted(project_dir.glob("*.jsonl"))


# ── Soma de usage ─────────────────────────────────────────────────


def sum_usage_from_transcript(path: Path) -> UsageSums:
    """Lê 1 JSONL, soma usage por categoria. JSONL malformado: skip + count."""
    sums = UsageSums()
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    sums.malformed_lines += 1
                    continue
                if not isinstance(d, dict):
                    continue
                msg = d.get("message")
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                # Turns sem input_tokens reais (ex: erro de API) podem ter usage vazia
                in_tok = int(usage.get("input_tokens") or 0)
                out_tok = int(usage.get("output_tokens") or 0)
                cr_tok = int(usage.get("cache_read_input_tokens") or 0)
                # Split cache_creation em 5m vs 1h via sub-objeto
                cache_creation = usage.get("cache_creation") or {}
                if not isinstance(cache_creation, dict):
                    cache_creation = {}
                c5m = int(cache_creation.get("ephemeral_5m_input_tokens") or 0)
                c1h = int(cache_creation.get("ephemeral_1h_input_tokens") or 0)
                # Fallback: se sub-objeto ausente mas top-level cache_creation_input_tokens
                # existir, atribui tudo a 5m (regra conservadora — 5m é o caso comum).
                if c5m == 0 and c1h == 0:
                    cc_total = int(usage.get("cache_creation_input_tokens") or 0)
                    if cc_total > 0:
                        c5m = cc_total
                sums.input_tokens += in_tok
                sums.output_tokens += out_tok
                sums.cache_read_tokens += cr_tok
                sums.cache_5m_write_tokens += c5m
                sums.cache_1h_write_tokens += c1h
                model = msg.get("model")
                if isinstance(model, str) and model:
                    sums.model_counts[model] = sums.model_counts.get(model, 0) + 1
                sums.valid_turns += 1
    except OSError:
        # Caller decide warning sobre arquivo ilegível
        pass
    return sums


def merge_usage(a: UsageSums, b: UsageSums) -> UsageSums:
    """Soma duas UsageSums (multi-session)."""
    merged_counts = dict(a.model_counts)
    for k, v in b.model_counts.items():
        merged_counts[k] = merged_counts.get(k, 0) + v
    return UsageSums(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cache_5m_write_tokens=a.cache_5m_write_tokens + b.cache_5m_write_tokens,
        cache_1h_write_tokens=a.cache_1h_write_tokens + b.cache_1h_write_tokens,
        cache_read_tokens=a.cache_read_tokens + b.cache_read_tokens,
        model_counts=merged_counts,
        malformed_lines=a.malformed_lines + b.malformed_lines,
        valid_turns=a.valid_turns + b.valid_turns,
    )


def dominant_model(sums: UsageSums) -> str | None:
    """Model com mais turns; None se nenhum modelo apareceu."""
    if not sums.model_counts:
        return None
    return max(sums.model_counts.items(), key=lambda kv: kv[1])[0]


def compute_cost(sums: UsageSums, model: str) -> float | None:
    """Calcula USD aplicando PRICING[model]. None se model desconhecido."""
    if model not in PRICING:
        return None
    p = PRICING[model]
    cost = (
        sums.input_tokens * p["input"]
        + sums.output_tokens * p["output"]
        + sums.cache_5m_write_tokens * p["cache_5m_write"]
        + sums.cache_1h_write_tokens * p["cache_1h_write"]
        + sums.cache_read_tokens * p["cache_read"]
    ) / 1_000_000.0
    return round(cost, 6)


# ── Entry point pro reconciler ─────────────────────────────────────


def compute_cost_for_ciclo(
    *,
    mmb_root: Path,
    repo: str,
    head_ref_name: str,
    claude_projects_root: Path,
    warn,
    ciclo_id: str = "?",  # só pra mensagem de warning
) -> CostResult | None:
    """Agrega cost + tokens para um ciclo a partir dos transcripts.

    Retorna None quando: head_ref_name fora da convenção, dir não existe,
    nenhum JSONL no dir. Esses casos resultam em cost/tokens = NULL na DB.

    Quando há multi-session: soma e emite warning explícito.
    Quando model desconhecido: cost_usd=None mas tokens preenchidos.
    Quando JSONL tem linhas malformadas: ignora + warning agregado.
    """
    transcripts = find_transcripts(
        mmb_root=mmb_root,
        repo=repo,
        head_ref_name=head_ref_name,
        claude_projects_root=claude_projects_root,
    )
    if not transcripts:
        warn(
            f"transcript-missing: ciclo={ciclo_id} repo={repo} "
            f"head={head_ref_name} — sem transcript no path esperado"
        )
        return None

    if len(transcripts) > 1:
        warn(
            f"transcript-multi-session: ciclo={ciclo_id} repo={repo} "
            f"head={head_ref_name} — {len(transcripts)} sessões no dir "
            f"(somadas; revisor humano pode validar)"
        )

    # Soma todas as sessões
    total = UsageSums()
    for p in transcripts:
        total = merge_usage(total, sum_usage_from_transcript(p))

    if total.malformed_lines > 0:
        warn(
            f"transcript-malformed-lines: ciclo={ciclo_id} "
            f"{total.malformed_lines} linha(s) JSONL inválida(s) ignoradas "
            f"(sessões: {len(transcripts)})"
        )

    if total.valid_turns == 0:
        warn(
            f"transcript-no-usage: ciclo={ciclo_id} {len(transcripts)} "
            f"sessão(ões) lidas mas nenhum turn com usage encontrado"
        )
        return None

    model = dominant_model(total)
    if len(total.model_counts) > 1:
        warn(
            f"transcript-mixed-model: ciclo={ciclo_id} modelos no "
            f"transcript: {total.model_counts} (usando dominante: {model})"
        )

    cost = compute_cost(total, model) if model else None
    if model and cost is None:
        warn(
            f"unknown-model: ciclo={ciclo_id} model={model} sem preço na "
            f"tabela — tokens preenchidos, cost_usd=NULL"
        )

    tokens_input = (
        total.input_tokens
        + total.cache_5m_write_tokens
        + total.cache_1h_write_tokens
        + total.cache_read_tokens
    )
    return CostResult(
        tokens_input=tokens_input,
        tokens_output=total.output_tokens,
        cost_usd=cost,
        model=model,
        sessions_count=len(transcripts),
    )
