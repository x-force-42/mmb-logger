"""Modelos Pydantic v2 do mmb-logger.

Espelham o contrato TypeScript que o Cockpit consome em
/MMB/mmb-cockpit/src/types/api.ts. Mantenha em sincronia.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

EpicoStatus = Literal["aberto", "fechado"]


class Epico(BaseModel):
    id: str
    slug: str
    started_at: str
    intencao: str
    status: EpicoStatus
    closed_at: str | None = None
    andaime_version: str | None = None
    ciclos_total: int = 0
    ciclos_completos: int = 0
    ciclos_abortados: int = 0


CicloStatus = Literal["iniciado", "planejado", "pr_aberto", "completo", "abortado"]
AbortOrigin = Literal[
    "heartbeat", "manual", "self", "master",
    "worker-exit", "worker-timeout", "stale",
]
MergedToMain = Annotated[int | None, Field(ge=0, le=1)]
AssertivenessScore = Annotated[int | None, Field(ge=1, le=5)]


class Ciclo(BaseModel):
    id: str
    epico_id: str
    project: str
    planner_invoked_at: str
    status: CicloStatus
    instruction: str
    pr_url: str | None = None
    pr_number: int | None = None
    closed_partial_at: str | None = None
    closed_complete_at: str | None = None
    merged_to_main: MergedToMain = None
    assertiveness_score: AssertivenessScore = None
    cost_usd: float | None = None
    abort_origin: AbortOrigin | None = None
    abort_reason: str | None = None
    andaime_version: str | None = None


class CicloDetail(Ciclo):
    briefing_md: str | None = None
    review_note: str | None = None
    abort_at: str | None = None
    tokens_input: int | None = None
    tokens_output: int | None = None
    diff_added: int | None = None
    diff_deleted: int | None = None
    diff_files: int | None = None


class EpicoDetail(Epico):
    ciclos: list[Ciclo] = Field(default_factory=list)


class CicloPatch(BaseModel):
    merged_to_main: MergedToMain = None
    assertiveness_score: AssertivenessScore = None
    review_note: str | None = None


EventoKind = Literal[
    "state_change",
    "msg_send",
    "msg_receive",
    "heartbeat_loss",
    "atomic_spawn",
    "atomic_deregister",
    "pr_opened",
    "journal_warn",
    "journal_error",
    "journal_critical",
]
EventoSeverity = Literal["info", "warn", "error", "critical"]


class Evento(BaseModel):
    id: int
    ciclo_id: str | None
    ts: str
    kind: EventoKind
    severity: EventoSeverity | None = None
    payload: dict = Field(default_factory=dict)


class Projeto(BaseModel):
    id: str
    slug: str
    name: str
    path: str
    repo_url: str | None = None
    created_at: str


class DiaCusto(BaseModel):
    dia: str
    usd: float


class DiaCiclos(BaseModel):
    dia: str
    n: int


class MetricasOverview(BaseModel):
    window_days: int
    ciclos_total: int
    epicos_total: int
    custo_total_usd: float
    tempo_medio_completo_s: float
    taxa_abort: float
    taxa_merged: float
    custo_por_dia: list[DiaCusto]
    ciclos_por_dia: list[DiaCiclos]
    status_breakdown: dict[CicloStatus, int]
    abort_breakdown: dict[AbortOrigin, int]


class EpicosListResponse(BaseModel):
    items: list[Epico]
    total: int
    limit: int
    offset: int


class CiclosListResponse(BaseModel):
    items: list[Ciclo]
    total: int
    limit: int
    offset: int


class ProjetosListResponse(BaseModel):
    items: list[Projeto]


class EventosListResponse(BaseModel):
    items: list[Evento]


EpicoDetail.model_rebuild()
