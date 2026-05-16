<!-- mmb-cycle-key: filtro-andaime-version/logger/2026-05-16T10:27:47Z
     mmb-briefing-file: 2026-05-16T10-27-47Z_master_briefing_filtro-andaime-version.md -->


# feat(api): filtro multiselect por andaime_version em /api/epicos e /api/ciclos

> **Body de sub-issue do GitHub.** Atômico lê isto como prompt direto.

## Contexto

Coluna `andaime_version` existe em `epicos` e `ciclos`, populada pelo
reconcile via `git describe`. API retorna no JSON mas não permite
filtrar. Rick precisa isolar histórico por versão do método (cross-repo
épico `filtro-andaime-version`).

## Intenção

Adicionar query param `andaime_version` **repetível** em `/api/epicos`
e `/api/ciclos`. Backend constrói `WHERE andaime_version IN (?,?,...)`.
Param ausente = sem filtro (compat atual preservada).

## Contrato (acordado no master-briefing)

```
GET /api/epicos?andaime_version=v0.5.0&andaime_version=v0.6.0
GET /api/ciclos?andaime_version=v0.6.0
GET /api/epicos                          # sem filtro
```

FastAPI sintaxe pra param repetível: `andaime_version: list[str] | None =
Query(default=None)`. Cliente repete a key, FastAPI agrega na lista.

## Escopo

### Dentro

- `src/mmb_logger/api/routes/epicos.py` — adicionar param.
- `src/mmb_logger/api/routes/ciclos.py` — idem.
- `src/mmb_logger/db.py` — `list_epicos` + `list_ciclos` aceitam
  `andaime_versions: list[str] | None = None` (kwarg) e geram WHERE
  IN dinâmico com placeholders parametrizados (NUNCA string concat).
- `tests/test_api.py` — 4 cenários (1 versão, 2+, vazio explícito, sem
  param).
- `tests/test_db.py` — 2 cenários no nível DB.

### Fora

- Frontend (mmb-cockpit task separada).
- Tag discovery endpoint.
- Validação contra lista enum (aceitar qualquer string; SQL IN cuida).
- Mudanças no schema da DB (campo já existe).

## Critério de pronto

- [ ] `GET /api/epicos?andaime_version=v0.6.0` retorna só épicos com
      `andaime_version="v0.6.0"`.
- [ ] `GET /api/epicos?andaime_version=v0.5.0&andaime_version=v0.6.0`
      retorna épicos com qualquer um dos dois valores.
- [ ] `GET /api/epicos` (sem param) retorna tudo (compat).
- [ ] Idem pra `/api/ciclos`.
- [ ] Filtro combina com filtros existentes via AND (ex:
      `?status=completo&andaime_version=v0.6.0` → completos sob v0.6.0).
- [ ] Testes locais passam (`uv run pytest -q`).
- [ ] Ruff clean (`uv run ruff check src tests`).
- [ ] Conventional Commits no PR.

## Contexto técnico

Arquivos relevantes:

- `src/mmb_logger/api/routes/epicos.py:13` — rota atual sem o param.
- `src/mmb_logger/api/routes/ciclos.py:14` — idem.
- `src/mmb_logger/db.py:218` — `list_epicos`.
- `src/mmb_logger/db.py:356` — `list_ciclos`.

Docs:
- `.tooling/source-of-truth.md` — `andaime_version` coluna derivada.

## Implementação sugerida (hint — você decide)

```python
# routes/epicos.py
@router.get("", response_model=EpicosListResponse)
def list_epicos_route(
    request: Request,
    status: str | None = Query(...),
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
    andaime_version: list[str] | None = Query(default=None),  # ← novo
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> EpicosListResponse:
    ...
    items, total = list_epicos(
        conn,
        status=status,
        date_from=date_from,
        date_to=date_to,
        andaime_versions=andaime_version,  # ← passa pro DB
        limit=limit,
        offset=offset,
    )
```

```python
# db.py — dentro de list_epicos / list_ciclos
if andaime_versions:
    placeholders = ",".join("?" for _ in andaime_versions)
    where.append(f"andaime_version IN ({placeholders})")
    params.extend(andaime_versions)
```

**Nunca** fazer string concat dos valores no SQL. SEMPRE placeholders.

## Testes a adicionar

### `tests/test_db.py`

- `test_list_epicos_filter_andaime_version_single` — 1 valor, retorna só
  os matching.
- `test_list_epicos_filter_andaime_version_multi` — 2+ valores, retorna
  union.
- Idem 2 testes para `list_ciclos`.

### `tests/test_api.py`

- `test_epicos_filter_andaime_version_single` — request com 1 valor.
- `test_epicos_filter_andaime_version_multi` — request com 2 valores.
- `test_epicos_filter_andaime_version_absent` — sem param, comportamento
  atual.
- `test_epicos_filter_andaime_version_combina_com_status` — AND com
  outro filtro.

## Dependências (cross-task)

Nenhuma. `requires: nenhum`.

## Conflito potencial com (outras tarefas)

Nenhum visível. Sem outra task tocando `routes/epicos.py`, `routes/ciclos.py`
ou `db.py` neste épico.

## Definition of Done

- [ ] Todos os itens de "Critério de pronto" check.
- [ ] PR linkou `Closes #<este-issue>` (open-pr.sh faz automático).
- [ ] Sub-issue fecha automaticamente após PR mergeado.
- [ ] Tests verde no CI (se aplicável) ou `pytest` local OK.

---

🤖 Issue criada pelo Orq de Projeto de `mmb-logger` a partir do briefing
em `.tooling/intents/2026-05-16-filtro-andaime-version/`.

