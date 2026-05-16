"""Testes da API HTTP."""

from __future__ import annotations

from fastapi.testclient import TestClient

from mmb_logger.db import get_conn, insert_evento, upsert_ciclo, upsert_epico, upsert_projeto


def _seed(db_path):
    with get_conn(db_path) as conn:
        upsert_epico(
            conn,
            id="ep1",
            slug="ep1",
            started_at="2026-05-10T10:00:00Z",
            intencao="x",
        )
        upsert_ciclo(
            conn,
            id="c1",
            epico_id="ep1",
            project="mmb-cockpit",
            planner_invoked_at="2026-05-10T10:00:00Z",
            status="iniciado",
            instruction="i",
            briefing_md="briefing",
        )
        insert_evento(
            conn,
            ciclo_id="c1",
            ts="2026-05-10T10:00:00Z",
            kind="msg_send",
            severity="info",
            payload={"foo": "bar"},
        )
        upsert_projeto(
            conn,
            id="mmb-cockpit",
            slug="mmb-cockpit",
            name="MMB Cockpit",
            path="/x",
            created_at="2026-05-10T10:00:00Z",
        )


def test_health(client: TestClient):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_health_route_unchanged(client: TestClient, db_path):
    # Garante que /api/health não foi quebrado pela introdução de
    # /api/health/detailed — shape antigo precisa permanecer exato.
    _seed(db_path)
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_health_detailed_route_200(client: TestClient):
    r = client.get("/api/health/detailed")
    assert r.status_code == 200


def test_health_detailed_route_shape(client: TestClient, db_path):
    r = client.get("/api/health/detailed")
    body = r.json()
    assert set(body.keys()) == {
        "status",
        "db_path",
        "ciclos_count",
        "projetos_count",
        "eventos_count",
    }
    assert body["status"] == "ok"
    assert isinstance(body["db_path"], str)
    assert isinstance(body["ciclos_count"], int)
    assert isinstance(body["projetos_count"], int)
    assert isinstance(body["eventos_count"], int)
    assert body["db_path"] == str(db_path)


def test_health_detailed_counts_correct(client: TestClient, db_path):
    _seed(db_path)
    body = client.get("/api/health/detailed").json()
    assert body["ciclos_count"] == 1
    assert body["projetos_count"] == 1
    assert body["eventos_count"] == 1


def test_list_epicos_vazio(client: TestClient):
    r = client.get("/api/epicos")
    assert r.status_code == 200
    body = r.json()
    assert body == {"items": [], "total": 0, "limit": 50, "offset": 0}


def test_list_epicos_com_dado(client: TestClient, db_path):
    _seed(db_path)
    r = client.get("/api/epicos")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == "ep1"
    assert body["items"][0]["ciclos_total"] == 1


def test_get_epico_detail(client: TestClient, db_path):
    _seed(db_path)
    r = client.get("/api/epicos/ep1")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "ep1"
    assert len(body["ciclos"]) == 1


def test_get_epico_404(client: TestClient):
    r = client.get("/api/epicos/inexistente")
    assert r.status_code == 404


def test_list_ciclos(client: TestClient, db_path):
    _seed(db_path)
    r = client.get("/api/ciclos?project=mmb-cockpit")
    assert r.status_code == 200
    assert r.json()["total"] == 1


def test_get_ciclo_detail(client: TestClient, db_path):
    _seed(db_path)
    r = client.get("/api/ciclos/c1")
    assert r.status_code == 200
    body = r.json()
    assert body["briefing_md"] == "briefing"


def test_patch_ciclo(client: TestClient, db_path):
    _seed(db_path)
    r = client.patch(
        "/api/ciclos/c1",
        json={"merged_to_main": 1, "assertiveness_score": 5, "review_note": "ótimo"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["merged_to_main"] == 1
    assert body["assertiveness_score"] == 5
    assert body["review_note"] == "ótimo"


def test_patch_ciclo_404(client: TestClient):
    r = client.patch("/api/ciclos/inexistente", json={"merged_to_main": 1})
    assert r.status_code == 404


def test_eventos_de_ciclo(client: TestClient, db_path):
    _seed(db_path)
    r = client.get("/api/ciclos/c1/eventos")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["kind"] == "msg_send"


def test_projetos(client: TestClient, db_path):
    _seed(db_path)
    r = client.get("/api/projetos")
    assert r.status_code == 200
    assert r.json()["items"][0]["slug"] == "mmb-cockpit"


def test_metricas_overview(client: TestClient, db_path):
    _seed(db_path)
    r = client.get("/api/metricas/overview?days=30")
    assert r.status_code == 200
    body = r.json()
    assert body["window_days"] == 30
    assert body["ciclos_total"] == 1


def _seed_andaime_versions(db_path):
    """Seed 3 épicos/ciclos com andaime_version distintos."""
    with get_conn(db_path) as conn:
        for idx, version in enumerate(["v0.5.0", "v0.6.0", "v0.7.0"], start=1):
            upsert_epico(
                conn,
                id=f"ep{idx}",
                slug=f"ep{idx}",
                started_at=f"2026-05-10T10:00:0{idx}Z",
                intencao="x",
                andaime_version=version,
            )
            upsert_ciclo(
                conn,
                id=f"c{idx}",
                epico_id=f"ep{idx}",
                project="mmb-cockpit",
                planner_invoked_at=f"2026-05-10T10:00:0{idx}Z",
                status="completo" if idx == 2 else "iniciado",
                instruction="i",
                andaime_version=version,
            )


def test_epicos_filter_andaime_version_single(client: TestClient, db_path):
    _seed_andaime_versions(db_path)
    r = client.get("/api/epicos?andaime_version=v0.6.0")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert {ep["id"] for ep in body["items"]} == {"ep2"}


def test_epicos_filter_andaime_version_multi(client: TestClient, db_path):
    _seed_andaime_versions(db_path)
    r = client.get("/api/epicos?andaime_version=v0.5.0&andaime_version=v0.6.0")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert {ep["id"] for ep in body["items"]} == {"ep1", "ep2"}


def test_epicos_filter_andaime_version_absent(client: TestClient, db_path):
    _seed_andaime_versions(db_path)
    r = client.get("/api/epicos")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3


def test_ciclos_filter_andaime_version_single(client: TestClient, db_path):
    _seed_andaime_versions(db_path)
    r = client.get("/api/ciclos?andaime_version=v0.7.0")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert {c["id"] for c in body["items"]} == {"c3"}


def test_ciclos_filter_andaime_version_multi(client: TestClient, db_path):
    _seed_andaime_versions(db_path)
    r = client.get("/api/ciclos?andaime_version=v0.5.0&andaime_version=v0.7.0")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert {c["id"] for c in body["items"]} == {"c1", "c3"}


def test_ciclos_filter_andaime_version_absent(client: TestClient, db_path):
    _seed_andaime_versions(db_path)
    r = client.get("/api/ciclos")
    assert r.status_code == 200
    assert r.json()["total"] == 3


def test_epicos_filter_andaime_version_combina_com_status(client: TestClient, db_path):
    """Filtro andaime_version combina com outros filtros via AND."""
    _seed_andaime_versions(db_path)
    # ep2 está aberto (default) e tem v0.6.0; pedimos status=aberto + v0.6.0.
    r = client.get("/api/epicos?status=aberto&andaime_version=v0.6.0")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == "ep2"

    # Filtro com versão que existe mas combinada com status que nenhum tem.
    r = client.get("/api/epicos?status=fechado&andaime_version=v0.6.0")
    assert r.status_code == 200
    assert r.json()["total"] == 0


def test_ciclos_filter_andaime_version_combina_com_status(client: TestClient, db_path):
    """AND entre andaime_version e status no /api/ciclos."""
    _seed_andaime_versions(db_path)
    # Só c2 tem status=completo; pedimos v0.6.0 (c2) + completo.
    r = client.get("/api/ciclos?status=completo&andaime_version=v0.6.0")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == "c2"

    # Mesmo versão v0.5.0 (c1, iniciado) + completo → vazio.
    r = client.get("/api/ciclos?status=completo&andaime_version=v0.5.0")
    assert r.status_code == 200
    assert r.json()["total"] == 0


def test_andaime_versions_route_200(client: TestClient, db_path):
    _seed_andaime_versions(db_path)
    r = client.get("/api/andaime-versions")
    assert r.status_code == 200


def test_andaime_versions_route_response_shape(client: TestClient, db_path):
    _seed_andaime_versions(db_path)
    r = client.get("/api/andaime-versions")
    body = r.json()
    assert set(body.keys()) == {"items"}
    # Semver-desc: v0.7.0 > v0.6.0 > v0.5.0
    assert body["items"] == ["v0.7.0", "v0.6.0", "v0.5.0"]


def test_andaime_versions_route_empty_db(client: TestClient):
    r = client.get("/api/andaime-versions")
    assert r.status_code == 200
    assert r.json() == {"items": []}


def test_cors(client: TestClient):
    r = client.options(
        "/api/health",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    # CORSMiddleware do FastAPI responde 200 com Access-Control-Allow-Origin.
    assert r.status_code in (200, 204)
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"
