<!-- mmb-cycle-key: logger-model-tracking/logger/2026-05-18T13:32:50Z
     mmb-briefing-file: logger-t3-backfill.md -->


# feat(logger): backfill heurístico do modelo Claude em ciclos (T3)

> **Body de sub-issue do GitHub.** Atômico lê isto como prompt direto.
>
> Continuação do épico `logger-model-tracking`. T2 (schema + ingest +
> API) mergeada via PR #20. Coluna `ciclos.model TEXT` agora vive em
> `main`. Esta task popula retroativamente o valor pra ciclos
> pré-T1 via inferência heurística por janela temporal.

## Contexto

Pós-T2, ciclos novos persistem `model` a partir do `state/agents.jsonl`
(evento `spawn` do planner). Ciclos pré-T1 ficam com `model IS NULL`.
Sem backfill, comparações histórico × novo perdem o eixo principal de
variação de custo (opus vs sonnet vs haiku ~10x em $/token).

Spec do escopo: `.tooling/intents/2026-05-18-logger-model-tracking/briefings/logger-model-tracking.md`,
seção "Escopo (T3) — backfill heurístico".

## Escopo

### 1. Janelas temporais de `MMB_MODE`

- Parsear `git log -- .tooling/config.sh` no andaime
  (`/home/eliezer/llab/MMB/.tooling/config.sh`) pra extrair as janelas
  temporais onde o default de `MMB_MODE` foi `normal` / `fast` /
  `balanced`.
- Mapear cada janela `[inicio, fim)` pra modelo do planner default
  daquele modo. Os ids exatos vêm do registry / config — confirmar
  no código antes de hard-codear.
- Se houver gap ou sobreposição entre janelas, registrar warning no
  journal e seguir.

### 2. Backfill em `ciclos`

- Pra cada ciclo com `model IS NULL AND closed_complete_at IS NOT NULL`:
  - Determinar a janela temporal correspondente via timestamp de
    referência do ciclo (sugestão: `started_at`; justifique a escolha
    no PR se usar outra coluna).
  - Janela bem-definida → set `model = <planner default daquela
    janela>`.
  - Janela ambígua (override por env-var não rastreável, intervalo
    sem cobertura, etc.) → manter `NULL` e emitir warning estruturado
    no journal (`/MMB/.tooling/logs/journal.jsonl`) com event-slug
    reutilizável (ex: `backfill-model-ambiguous`, payload com
    `cycle_id` + motivo).
- Não tocar ciclos `abortado` que existam antes da janela
  `MMB_MODE=normal` ser o default — identifique essa borda e
  justifique no PR.

### 3. Idempotência e dry-run

- Idempotente: re-rodar não regride dados já populados, não duplica
  warnings, não reverte humanos (`assertiveness_score`, `review_note`
  continuam intocados — contrato derivado vs humano em
  `.tooling/source-of-truth.md`).
- Flag `--dry-run`: imprime quantos ciclos seriam backfilled, quantos
  ficariam NULL com warning, mas **não escreve** no DB nem no journal.

### 4. Execução em produção

- Após merge, rodar uma vez no DB local de produção (`mmb-logger.db`)
  com `--dry-run`, validar contagens, depois rodar de verdade.
- Resultado (counts + sample de warnings) entra como comment no PR
  ou no comment de fechamento da sub-issue.

## Entrega

- Subcomando CLI sugerido: `uv run mmb-logger backfill-model [--dry-run]`
  (atômico escolhe nome final coerente com `cli.py`).
- Módulo novo em `src/mmb_logger/reconcile/` ou
  `src/mmb_logger/backfill/` (coerência com a estrutura existente
  prevalece).

## Não-objetivos

- Não criar UI no cockpit.
- Não forçar `NOT NULL` em `model` no schema.
- Não rastrear modelos de `claude -p` ad-hoc fora do registry.
- Não tocar ciclos onde `closed_complete_at IS NULL` (em-voo).

## Definition of done

- Backfill executado pelo menos uma vez no DB local de produção;
  warnings logados no journal pros NULLs justificáveis.
- Testes cobrindo:
  - Idempotência (rodar 2x = mesmo resultado).
  - Janela temporal correta (ciclo em janela X → modelo Y).
  - Warning emitido em caso ambíguo (sem write no DB pra esse ciclo).
  - `--dry-run` não muta o DB.
- Suíte verde no PR (guardrail A11) — `MMB_SUITE_OUTPUT` apontando
  pra output literal de `uv run pytest -q` passando.
- PR body com `Closes #21` + seção "Suíte verde".
- Atômico nunca mergeia (guardrail A10).
- `status: task-fechada-<id>` final do épico com `last_in_epic: true`.

## Conflito potencial

Nenhum conhecido. Mudanças aditivas em coluna existente, reconciler
v3 tolera campos novos via `source_key`. Domínio derivado vs humano
respeitado (`model` é derivado).
