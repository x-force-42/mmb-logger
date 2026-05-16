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
