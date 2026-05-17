# mmb-logger

Sistema de logs do **andaime MMB**. Lê de forma passiva os 3 fluxos
estruturados produzidos pelo andaime e persiste em SQLite. Expõe API
HTTP (FastAPI) que o Cockpit consome retrospectivamente.

## O que ele observa

- `inbox/<dest>/*.md` — mensagens entre agentes (master / planners),
  parseadas via frontmatter YAML.
- `logs/journal.jsonl` — eventos warn/error/critical do andaime.
- `state/agents.jsonl` — eventos spawn/deregister/heartbeat de atômicos.

## Modelo conceitual

`1 épico → N ciclos → N eventos`. Cada ciclo é uma invocação de
planner de projeto sobre um repo. Eventos brutos são parseados e
regras de inferência (R1-R10) transitam o ciclo entre os estados
`iniciado → planejado → pr_aberto → completo` (ou `abortado`).

## Instalação

Pré-requisito: [`uv`](https://docs.astral.sh/uv/).

```bash
cd /home/eliezer/llab/MMB/mmb-logger
uv sync
```

## Uso

```bash
# Inicializar/aplicar schema. Idempotente.
uv run mmb-logger init-db

# Varredura única do histórico existente.
uv run mmb-logger ingest-once

# Modo watch: varre uma vez (catch-up) e observa mudanças.
uv run mmb-logger watch

# Sobe API + cron interno do reconcile.
uv run mmb-logger serve
# default: http://127.0.0.1:8765 — docs em /docs

# Trigger imediato sem esperar o tick do cron:
curl -X POST http://127.0.0.1:8765/api/reconcile
# Status do cron:
curl http://127.0.0.1:8765/api/reconcile-status
```

A CLI `uv run mmb-logger reconcile` continua sendo a **fonte de verdade
operacional pra debug/emergência**. O cron interno do `serve` é apenas
conveniência: chama a mesma função. Se o `serve` estiver caído, não há
reconcile automático — use a CLI.

### Variáveis de ambiente

- `MMB_LOGGER_DB_PATH` — caminho do SQLite (default: `./mmb-logger.db`).
- `MMB_LOGGER_TOOLING_PATH` — raiz do `.tooling/` (default: `/home/eliezer/llab/MMB/.tooling`).
- `MMB_LOGGER_RECONCILE_AUTO` — `0` desliga o cron interno (default ligado).
- `MMB_LOGGER_RECONCILE_INTERVAL` — intervalo do cron em segundos (default `300`).

## Desenvolvimento

```bash
uv run pytest -v
uv run ruff check src tests
uv run ruff format --check src tests
```
