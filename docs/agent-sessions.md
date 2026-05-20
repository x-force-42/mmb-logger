# `agent_sessions` — camada retrospectiva de sessões Claude Code

Esta tabela materializa **uma linha por sessão** Claude Code, lida dos
transcripts em `~/.claude/projects/<encoded-path>/<uuid>.jsonl`. É populada
por um comando opt-in (`mmb-logger backfill-agent-sessions`), nunca pelo
`reconcile` padrão nem pelo cron interno.

> **TL;DR:** dados sobre uso de IA, separados de `ciclos`/`epicos`. Custo
> é estimativa explícita. ORPHAN é estado válido, não erro.

## Quando usar

- Para perguntar coisas que **não cabem em `ciclos`**: uso de IA por role
  (master vs orq vs atomic vs dev manual), profile de tool calls, tempo
  ativo vs idle, etc.
- Para auditar **sessões fora do andaime** (debug interativo, Master,
  sessões manuais) que o reconcile não captura.

## Como popular

```bash
# Simulação (sem escrever no DB)
uv run mmb-logger backfill-agent-sessions --dry-run

# Persistir
uv run mmb-logger backfill-agent-sessions --write

# Offline (sem chamar gh — atomics ficam LOW)
uv run mmb-logger backfill-agent-sessions --dry-run --no-gh

# Exportar dataset pra inspeção
uv run mmb-logger backfill-agent-sessions --dry-run \
  --output-jsonl /tmp/sessions.jsonl
```

**Fail-safe:** sem `--dry-run` nem `--write`, o comando recusa rodar com
mensagem de ajuda. Sem caminho silencioso.

**Idempotente:** rerun com `--write` não duplica linhas (UPSERT por
`session_id`). Reprocessa colunas derivadas com o pricing/heurística
correntes.

## Conceitos importantes

### `role` ≠ `link_confidence`

Duas dimensões ortogonais. Não confundir:

| Dimensão | Valores | Significado |
|---|---|---|
| `role` | `master \| manual \| atomic \| orchestrator \| unknown` | **Onde** a sessão rodou (path encoded). |
| `link_confidence` | `HIGH \| MEDIUM \| LOW \| ORPHAN` | **Quão certo** está o link com `ciclos`/`epicos`. |

Exemplo: `role='master'` sempre é `link_confidence='ORPHAN'` por design
(Master é cross-cycle por natureza). `role='atomic'` pode ser HIGH (PR
+ âncora `mmb-cycle-key`), MEDIUM (PR sem âncora), LOW (sem PR) ou ORPHAN
(em modo `--no-gh`).

### `ORPHAN` é estado válido, **não erro**

Significa "sem ciclo MMB único confiável". Casos legítimos:
- Sessões Master — orquestram múltiplos ciclos, não pertencem a um só.
- Sessões manuais (dev tradicional sem worktree do andaime).
- Sessões pré-andaime / smoke tests sem PR.

ORPHAN é persistido com `ciclo_id = NULL` e tem `link_reason` explicando.
Filtrar por `link_confidence != 'ORPHAN'` é uma decisão de consulta, não
de schema.

### Custo: `cost_usd_estimated`, **nunca** `cost_usd`

O custo desta tabela é uma estimativa baseada em tabela de preços local
(`PRICING` em `reconcile/transcripts.py`). Nunca substitui o cálculo
existente de `ciclos.cost_usd`. Características:

- Coluna se chama `cost_usd_estimated` para deixar explícito.
- `cost_pricing_version` é carimbado por linha (default: `2026-05-16`).
- `cost_confidence` rotula o grau de confiança:

  | valor | quando |
  |---|---|
  | `ok_unvalidated` | model conhecido, único, sem synthetic — caminho mais alto sem validação contra Console Anthropic |
  | `mixed_models` | múltiplos models na mesma sessão — soma é correta mas pode mascarar trocas |
  | `has_synthetic` | sessão tem turns com `model=<synthetic>` (compaction interna do Claude Code) — tokens já foram cobrados na conta |
  | `unknown_model` | model fora da PRICING — `cost_usd_estimated=NULL` |
  | `no_model` | nenhum model identificado — `cost_usd_estimated=NULL` |

Pra promover estimativa → fato oficial, é necessário validação manual de
1-2 sessões contra o Console da Anthropic. Esse trabalho é separado e
fora do escopo do backfill.

### Durações: 3 visões

| Coluna | Significado | Quando preencher |
|---|---|---|
| `duration_wall_ms` | `ended_at − started_at`. Inclui idle. | Sempre que calculável (≥99%). |
| `active_interaction_ms` | Soma de gaps entre turns consecutivos abaixo de `IDLE_GAP_THRESHOLD_MS` (5 min). Proxy de engajamento. | Sempre que há ≥2 turns. |
| `duration_api_ms` | Tempo gasto pela API gerando resposta (verdade oficial via `claude -p --output-format json`). | **NULL retroativo.** Só preencher quando vier da fonte canônica no futuro. |

**Atenção:** uma sessão Master de 42h de `duration_wall_ms` **não** é 42h
de trabalho contínuo. Cockpit/relatórios devem preferir
`active_interaction_ms` ou `num_turns` como proxy de engajamento.

### `task_id_raw` vs `task_id_normalized`

O encoding do Claude Code substitui `.` por `-` no nome do diretório.
Branch original: `task/1.1-blocos-progresso`. Dir encoded:
`...--worktrees-1-1-blocos-progresso`.

- `task_id_raw` preserva a forma encoded (`1-1`).
- `task_id_normalized` é promovido a `1.1` quando a branch é evidência.
- A regra aplicada vai em `evidence_json.task_id_norm_rule`.

Use `task_id_normalized` pra cruzar com branches/PRs. Use `task_id_raw`
pra cruzar com paths no filesystem.

## Relação com `ciclos`/`epicos`

- `ciclo_id` é **FK opcional** com `ON DELETE SET NULL`. ORPHAN ingere
  com NULL; `reconcile --reset` (que dropa `ciclos`+`epicos`) **não**
  derruba `agent_sessions`, apenas zera os FKs.
- O backfill resolve `ciclo_id`/`epico_id` best-effort consultando o DB
  local pelo `pr_number` quando atomic. Sem PR, fica NULL.
- **Não há agregação automática** que sobrescreva `ciclos.cost_usd` a
  partir de `agent_sessions.cost_usd_estimated`. Decisão explícita —
  domínios separados.

## Não acoplamento ao reconcile

- O backfill **não roda** durante `reconcile`.
- O **cron interno** do `serve` (que dispara reconcile a cada N segundos)
  **não dispara** o backfill.
- O endpoint `POST /api/reconcile` **não dispara** o backfill.
- O comando é exclusivamente CLI. Crescimento futuro pode adicionar
  endpoint/cron, mas exige decisão explícita.

## Linkagem GitHub

Pra atomics, o backfill cruza `git_branch` com PRs via `gh pr list` (1
chamada por target). PR achado leva à issue casada (`closingIssuesReferences`),
que tem o body inspecionado para a âncora `mmb-cycle-key`:

```html
<!-- mmb-cycle-key: <epic_slug>/<project_short>/<briefing_created_ts>
     mmb-briefing-file: <basename do briefing> -->
```

- Âncora encontrada → HIGH, `mmb_cycle_key` vai em `evidence_json`.
- PR sem âncora → MEDIUM.
- Sem PR → LOW.

Modo `--no-gh` pula tudo isso; atomics caem em LOW por design.

## Limitações conhecidas

1. **`duration_api_ms` retroativo é NULL**. Proxy de transcript é fraca
   (gaps user→assistant podem incluir reflexão humana). Só preencher
   quando vier de fonte canônica.
2. **Pricing pode estar defasado**. `PRICING` em `transcripts.py` é
   datada (`2026-05-16`); preço público pode mudar. `cost_pricing_version`
   é gravado por linha pra forensics.
3. **GH offline ou rate limited**: `--no-gh` é fallback; atomics ficam
   LOW. Repare em runs intermitentes que mudam confiança entre execuções.
4. **`mmb-core` é histórico**. Não está em `targets.json` mas existe em
   paths antigos. O backfill inclui esse repo na busca de PRs pra cobrir
   atomics anteriores ao rename.
5. **Sessões com resume** (mesma worktree, múltiplos `.jsonl`): cada
   arquivo é uma sessão separada. Agregação é decisão do consumidor.
6. **Transcript schema do Claude Code** pode mudar em versões futuras.
   `claude_version` é gravado por linha pra forensics.

## Schema

Definição canônica em [`/schema.sql`](../schema.sql) (busque por
`agent_sessions`). Resumo dos campos em [`/schema.sql`](../schema.sql).

Domínio derivado: `DERIVED_COLS` em
[`src/mmb_logger/backfill/agent_sessions.py`](../src/mmb_logger/backfill/agent_sessions.py)
— todas as colunas escrevíveis pelo backfill. Espaço pra colunas humanas
no futuro está reservado pelo pattern UPSERT seletivo.
