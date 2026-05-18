<!-- mmb-cycle-key: logger-model-tracking/logger/2026-05-18T13:00:57Z
     mmb-briefing-file: 2026-05-18T13-00-57Z_master_briefing_logger-model-tracking.md -->


# feat(logger): persistir modelo Claude em ciclos (T2 — schema + ingest + API)

> **Body de sub-issue do GitHub.** Atômico lê isto como prompt direto.
>
> **Escopo desta issue: somente T2** (schema + ingestion + API).
> T3 (backfill heurístico) é uma **sub-issue separada** que abrirá
> depois desta mergear. Não execute T3 aqui.

## Contexto

Hoje `ciclos` persiste `cost_usd`, `tokens_input/output`, mas não o
**modelo Claude usado**. Sem isso, comparações de custo/qualidade
entre ciclos perdem o eixo principal de variação (opus vs sonnet vs
haiku é ~10x em $/token). Memória do método já antecipa experimento
"modelo menor em atômicos" — sem captura, o experimento não é
mensurável.

A informação **vai começar a chegar** no `state/agents.jsonl` como
campo `model` em eventos `spawn` (tarefa T1 do andaime — executada
pelo Mestre fora deste repo, antes ou em paralelo a esta task).

Master-briefing completo:
`/MMB/.tooling/intents/2026-05-18-logger-model-tracking/master-briefing.md`.

## Escopo

### T2.1 — Schema migration

- Em `schema.sql` e em `db.py::init_db`:
  - `ALTER TABLE ciclos ADD COLUMN model TEXT` (NULL permitido).
  - Migration idempotente (padrão `ALTER TABLE` defensivo já existe
    em `init_db`).
- **Não** adicionar índice ainda — esperar uso real revelar
  necessidade.

### T2.2 — Ingestion

- Parser de `state/agents.jsonl` lê novo campo `model` quando
  presente em eventos `spawn`.
- Política de mapeamento `agent → ciclo.model`:
  - Modelo do ciclo = modelo do agente **planner do projeto**
    (id típico: `core` / `cockpit` / `aquarium` / `logger`).
  - Se houver múltiplos spawns do mesmo planner no mesmo ciclo
    (raro), pegar o primeiro com `model` presente.
  - Atômicos têm modelo próprio, registrado em `agents.jsonl`,
    mas **não** populam `ciclos.model`. (Granularidade por
    agente — atômicos ficam visíveis via endpoint de agentes;
    ciclo guarda só o do planner.)
- Tolerância: `model` ausente → `NULL`. Sem warning, sem erro.
- Adicionar `model` à `DERIVED_COLS` em `reconcile/reconcile.py`
  (UPSERT seletivo — o reconciler escreve, humano não toca).

### T2.3 — API

- Endpoint(s) que retornam ciclos devem expor `model` no payload.
- Endpoint de agentes (se existir; se não, fora de escopo aqui)
  expõe `model` por agente.
- Consultar contrato atual da API em
  `src/mmb_logger/api/routes/` antes de definir shape exato.
- Atualizar `models.py` (Pydantic) pra incluir `model: str | None`.

## Não-objetivos

- **T3 (backfill heurístico)** — sub-issue própria, depende desta
  mergear. NÃO incluir aqui.
- Não capturar `effort` (épico separado eventual).
- Não criar UI no cockpit.
- Não forçar `NOT NULL` em `model`.
- Não rastrear modelo de `claude -p` ad-hoc fora dos agentes
  registrados.

## Critério de pronto

- [ ] `ciclos.model TEXT` adicionada em `schema.sql` + `init_db`
      (idempotente — re-rodar `init-db` em DB já migrado não falha).
- [ ] Parser de `agents.jsonl` extrai `model` de eventos `spawn`.
- [ ] `reconcile` popula `ciclos.model` com modelo do planner
      (primeiro spawn do planner do projeto, com `model` presente).
- [ ] `model` exposto em payload de `/api/ciclos` (e/ou `/api/epicos`
      se aplicável — verificar shape atual).
- [ ] `model` exposto por agente, se houver endpoint de agentes hoje.
- [ ] Tolerância a NULL: ciclos sem `model` no JSONL ingestam sem
      warning/erro.
- [ ] Testes cobrindo: campo presente, campo ausente (NULL),
      múltiplos spawns do planner (pega o primeiro com model).
- [ ] `DERIVED_COLS` em `reconcile/reconcile.py` inclui `model`.
- [ ] `uv run pytest -q` verde.
- [ ] `uv run ruff check src tests` clean.

## Critérios pro PR (guardrails)

- A11: Suíte verde — `MMB_SUITE_OUTPUT` apontando pra output literal
  de `pytest -q` passando.
- PR body com `Closes #<este-issue>` (open-pr.sh faz automático) +
  seção "Suíte verde".
- A10: Atômico **nunca** mergeia. Apenas abre PR.

## Conflito potencial

Nenhum conhecido. Mudanças aditivas, idempotentes, e o reconciler v3
já é robusto a campos novos via `source_key`.

## Dependências (cross-task)

- **T1 (andaime)**: campo `model` emitido em eventos `spawn` do
  `agents.jsonl`. Se T1 ainda não rodou em produção, o parser deve
  apenas tolerar ausência (NULL) — testes de ingestão devem cobrir
  ambos os cenários.
- **T3 (backfill)**: sub-issue separada, depende deste PR mergear.

## Contexto técnico

Arquivos relevantes (read-only pra orientar; atômico decide o
recorte exato):

- `schema.sql` — schema canônico da tabela `ciclos`.
- `src/mmb_logger/db.py::init_db` — migrations idempotentes.
- `src/mmb_logger/models.py` — Pydantic mirror da DB.
- `src/mmb_logger/ingest/` — parsers do `agents.jsonl`.
- `src/mmb_logger/reconcile/reconcile.py` — `DERIVED_COLS`.
- `src/mmb_logger/api/routes/ciclos.py` — endpoint de ciclos.
- `.tooling/source-of-truth.md` — contrato canônico das colunas
  derivadas.

---

🤖 Issue criada pelo Orq de Projeto de `mmb-logger` a partir do
briefing em `.tooling/intents/2026-05-18-logger-model-tracking/`.
