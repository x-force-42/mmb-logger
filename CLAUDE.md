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
uv run mmb-logger serve              # API REST em :8765
uv run pytest -q                     # testes
uv run ruff check src tests          # lint
uv run ruff check --fix src tests    # autofix
```

## Domínios na DB

Documentado em `.tooling/source-of-truth.md`:

- **Derivado** (reconciler escreve, UPSERT seletivo): tudo que vem dos
  artefatos canônicos. Idempotente.
- **Humano** (cockpit PATCH escreve, reconciler **nunca** toca):
  `assertiveness_score`, `review_note`.

Constante `DERIVED_COLS` em `reconcile/reconcile.py` é a única lista
permitida no UPSERT. Adicionar coluna nova ao domínio derivado exige
editar essa constante + teste de preservação humana.

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
