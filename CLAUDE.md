# CLAUDE.md — mmb-logger

Guia para sessões Claude que operam neste repo.

## O que é este projeto

**mmb-logger** é o sistema de observabilidade retrospectiva do ecossistema
MMB. Lê artefatos canônicos produzidos pelo andaime (GitHub issues/PRs,
inbox de briefings, journal, agents.jsonl, transcripts Claude, intents/)
e materializa o estado em SQLite via comando `reconcile`. Expõe API REST
(FastAPI) que o cockpit consome.

Stack: Python 3.12+ · FastAPI · SQLite · uv · pytest.

## Quem você é nesta sessão

Depende do contexto onde foi spawnado:

- **Sessão Claude na raiz `/MMB/mmb-logger/`** disparada pelo `commd`
  (worker stateless) → você é o **orq local do logger**. Recebeu uma
  mensagem em `.tooling/inbox/logger/`. Leia
  [`.tooling/profiles/project-orchestrator.md`](../.tooling/profiles/project-orchestrator.md)
  como instrução autoritativa de comportamento. Você cria sub-issue
  no GitHub via `.tooling/bin/create-task-issue.sh`, spawna atômico
  via `.tooling/bin/spawn-atomic.sh`, reporta status pro master via
  `msg.sh`.
- **Sessão Claude em uma worktree** (`.worktrees/<id>-<slug>/`)
  spawnada por `spawn-atomic.sh` → você é um **agente atômico**. Leia
  [`.tooling/profiles/atomic-agent.md`](../.tooling/profiles/atomic-agent.md).
  Sua tarefa está descrita no body da sub-issue do GitHub.
- **Sessão Claude interativa rodada manualmente pelo Rick** → você não
  é orq nem atômico. Trabalho dev tradicional. Responda o que ele
  pedir.

## Convenções do projeto

- **Versionamento**: gerenciado por `uv`. Use `uv sync`, `uv run`,
  `uv run pytest`.
- **Lint**: `uv run ruff check src tests`. Lint-clean é precondição
  de PR.
- **Testes**: `uv run pytest -q`. Suite completa precisa estar verde.
  Testes usam DB em `tmp_path` (fixture `db_path` em conftest).
- **Schema migrations**: idempotentes em `db.py::init_db` (ALTER TABLE
  defensivo + table-rebuild dance quando necessário). Não use Alembic.
- **Reconcile é one-shot**: nunca adicionar polling/watch sem caso de
  uso concreto. Idempotência é princípio.
- **Source-of-truth**: o contrato canônico do que cada coluna projeta
  e de qual fonte vive em [`/MMB/.tooling/source-of-truth.md`](../.tooling/source-of-truth.md).
  Antes de tocar coluna do DB, ler.

## Estrutura

```
mmb-logger/
├── pyproject.toml
├── schema.sql                  ← schema canônico aplicado por init_db
├── src/mmb_logger/
│   ├── cli.py                  ← typer: version | init-db | reconcile | serve
│   ├── db.py                   ← conexão + migrations + UPSERT seletivo
│   ├── models.py               ← Pydantic v2 (espelha api.ts do cockpit)
│   ├── api/
│   │   ├── app.py              ← FastAPI factory + CORS
│   │   └── routes/             ← /api/{epicos,ciclos,eventos,projetos,metricas}
│   ├── ingest/                 ← parsers (inbox, journal, agents, frontmatter)
│   └── reconcile/              ← gh, inbox, abort, audit, intents, transcripts
└── tests/                      ← pytest
```

## Comandos comuns

```bash
uv sync                              # instala deps
uv run mmb-logger init-db            # cria/migra schema
uv run mmb-logger reconcile          # projeta GH + filesystem → DB
uv run mmb-logger reconcile --reset  # ⚠ DESTRUTIVO — drop ciclos+epicos
uv run mmb-logger serve              # API REST em :8765 (+ cron interno)
uv run pytest -q                     # testes
uv run ruff check src tests          # lint
uv run ruff check --fix src tests    # autofix
```

## Reconcile automático (v0.4+)

O `serve` mantém um **cron interno** que dispara `reconcile()` periodicamente,
pra reduzir a fricção de rodar `mmb-logger reconcile` à mão sempre que algo
muda no andaime. A CLI manual continua sendo a **fonte de verdade operacional
pra debug/emergência** — todos os caminhos automáticos invocam a mesma
função `reconcile()`.

| Mecanismo | Como dispara | Quando usar |
|---|---|---|
| Cron interno (default) | A cada `MMB_LOGGER_RECONCILE_INTERVAL` segundos (default `300`) | Conveniência. Liga junto com `serve` |
| `POST /api/reconcile` | Trigger manual via HTTP | Cockpit, scripts, debug rápido sem esperar o tick |
| `uv run mmb-logger reconcile` | CLI | Debug, CI, ambientes sem `serve` rodando, **emergência** |

Env:
- `MMB_LOGGER_RECONCILE_AUTO=0` desliga o cron interno (default ligado).
- `MMB_LOGGER_RECONCILE_INTERVAL=<seg>` ajusta o intervalo.

Observabilidade:
- `GET /api/reconcile-status` → `auto_enabled`, `interval_seconds`, `running`,
  `last_run_ts`, `last_status` (`ok|error`), `last_error_msg`, `last_result`.
- Falha do reconcile **não derruba o serve**. Próximo tick continua tentando.
- Se o `serve` cair, **não há reconcile automático** — use a CLI ou suba o
  `serve` de novo. O endpoint manual também fica indisponível.

## Domínios na DB

Documentado em `.tooling/source-of-truth.md`:

- **Derivado** (reconciler escreve, UPSERT seletivo): tudo que vem dos
  artefatos canônicos. Idempotente.
- **Humano** (cockpit PATCH escreve, reconciler **nunca** toca):
  `assertiveness_score`, `review_note`.

Constante `DERIVED_COLS` em `reconcile/reconcile.py` é a única lista
permitida no UPSERT. Adicionar coluna nova ao domínio derivado exige
editar essa constante + teste de preservação humana.

## Convenção de fuso (v0.9.0+)

**Storage:** todos os timestamps em colunas do DB são **UTC** (ISO 8601
com `Z`). Não muda.

**Agregação diária em `/api/metricas/overview`:** as métricas
`custo_por_dia` e `ciclos_por_dia` usam **dia operacional local do MMB,
atualmente BRT/UTC-3**, derivado via SQLite modifier `datetime(ts, '-3
hours')`. Eventos rodados entre 21:00–23:59 BRT continuam contabilizados
no dia BRT correto (e não no dia UTC seguinte como antes).

Esta convenção **não se generaliza** automaticamente pra outros campos
de data do sistema — só vale pro bucketing diário da view de métricas.
Outros endpoints/queries que vierem a bucketar por dia devem decidir
explicitamente entre UTC e local; sem decisão, default é UTC (estado
nativo do storage).

Hardcode `-3 hours` é decisão MVP/monolocal. Promover pra env var
(`MMB_LOGGER_TZ_OFFSET`) se: (a) Brasil voltar a usar DST, ou (b) a
operação ficar multi-timezone.

## Quando NÃO seguir o profile estrito

- Rick pediu hotfix direto (1 linha) — modo dev tradicional.
- Refactor exploratório — conversa direta sem ritual.
- Você é o **master interativo do MMB** rodando em outro contexto
  editando este repo — modo refactor cross-repo do master (raro).

## Camada agêntica — fonte da verdade

| Coisa | Onde mora |
|---|---|
| Trabalho em-voo (issues/PRs) | GitHub (consultável via `gh`) |
| Estado retrospectivo materializado | `mmb-logger.db` (SQLite local) |
| Briefings recebidos | `.tooling/inbox/logger/` |
| Contrato canônico | `.tooling/source-of-truth.md` |
| Convenções de método | `.tooling/profiles/*.md` |
