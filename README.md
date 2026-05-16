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

# Sobe API.
uv run mmb-logger serve
# default: http://127.0.0.1:8765 — docs em /docs
```

### Variáveis de ambiente

- `MMB_LOGGER_DB_PATH` — caminho do SQLite (default: `./mmb-logger.db`).
- `MMB_LOGGER_TOOLING_PATH` — raiz do `.tooling/` (default: `/home/eliezer/llab/MMB/.tooling`).

## Desenvolvimento

```bash
uv run pytest -v
uv run ruff check src tests
uv run ruff format --check src tests
```
