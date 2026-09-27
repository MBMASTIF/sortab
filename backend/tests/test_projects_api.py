"""End-to-end HTTP tests for "мягкий email" identification + Проекты
(Postgres in production, in-memory SQLite here — see backend/db.py for why
that swap is safe: identical schema/queries via SQLAlchemy Core, only the
engine changes, the same "swap the dependency, not the code" principle
backend/tests/test_api.py already uses for Redis vs fakeredis).

Covers the actual point of the whole feature end to end: upload a file,
build a tree, save it as a Project, then re-upload a DIFFERENT file for
the same Project and confirm previously-assigned entities are recognized
automatically while genuinely new ones still require manual sorting (via
core.tree.detect_new_entities, exercised through the real HTTP endpoint).
Also covers the required failure paths: no cookie -> 401, unknown project
-> 404, and the free-tier "1 project" limit -> 409 (not a silent no-op).
"""

from decimal import Decimal
from io import BytesIO

import fakeredis
import openpyxl
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from backend.db import metadata
from backend.main import app, get_db, get_redis


@pytest.fixture
def client():
    fake_redis = fakeredis.FakeStrictRedis()
    test_engine = sa.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    metadata.create_all(test_engine)

    app.dependency_overrides[get_redis] = lambda: fake_redis
    app.dependency_overrides[get_db] = lambda: test_engine
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _xlsx_bytes(rows: list[list[str]]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _upload_and_finalize(client, rows: list[list[str]]) -> str:
    """rows[0] is the header — uploads, then immediately picks
    header_row_index=0, entity_column=0, metric_column=1."""
    resp = client.post(
        "/api/upload",
        files={"file": ("report.xlsx", _xlsx_bytes(rows), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert resp.status_code == 200, resp.text
    session_id = resp.json()["session_id"]

    columns_resp = client.post(
        f"/api/session/{session_id}/columns",
        json={"header_row_index": 0, "entity_column": 0, "metric_column": 1},
    )
    assert columns_resp.status_code == 200, columns_resp.text
    return session_id


FILE_A = [
    ["Товар", "Сумма"],
    ["носки чёрные", "4500"],
    ["ботинки зимние", "32000"],
    ["шапка вязаная", "8900"],
]

# Re-upload for the same recurring report: 2 previously-assigned entities
# reappear verbatim, "шапка вязаная" reappears but was never assigned in
# the saved project (so it's still "new" — see the design note in
# backend/main.py::load_project_into_session), plus one genuinely
# never-seen-before entity.
FILE_B = [
    ["Товар", "Сумма"],
    ["носки чёрные", "5000"],
    ["ботинки зимние", "31000"],
    ["шапка вязаная", "9200"],
    ["трусы муж.", "3400"],
]


def test_projects_endpoints_require_identification(client):
    session_id = _upload_and_finalize(client, FILE_A)

    resp = client.post("/api/projects", json={"session_id": session_id, "name": "Мой проект"})
    assert resp.status_code == 401

    assert client.get("/api/projects").status_code == 401
    assert client.get("/api/projects/does-not-exist").status_code == 401
    assert (
        client.post(f"/api/session/{session_id}/load-project/does-not-exist").status_code == 401
    )


def test_identify_sets_cookie_and_is_idempotent_per_email(client):
    resp = client.post("/api/auth/identify", json={"email": "  Owner@Example.com "})
    assert resp.status_code == 200
    assert resp.json()["email"] == "owner@example.com"
    assert "gruper_session" in resp.cookies

    # calling again with the same (differently-cased/whitespaced) email
    # must resolve to the same underlying user, not create a duplicate
    resp2 = client.post("/api/auth/identify", json={"email": "owner@example.com"})
    assert resp2.status_code == 200


def test_identify_rejects_garbage_email(client):
    resp = client.post("/api/auth/identify", json={"email": "not-an-email"})
    assert resp.status_code == 400


def test_save_project_requires_finalized_session(client):
    resp = client.post(
        "/api/upload",
        files={"file": ("report.xlsx", _xlsx_bytes(FILE_A), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    session_id = resp.json()["session_id"]

    client.post("/api/auth/identify", json={"email": "a@example.com"})
    # columns were never set on this session
    resp = client.post("/api/projects", json={"session_id": session_id, "name": "Проект"})
    assert resp.status_code == 409


def test_full_project_lifecycle_and_reupload_reuses_dictionary(client):
    session_id = _upload_and_finalize(client, FILE_A)

    client.post("/api/auth/identify", json={"email": "owner@example.com"})

    client.post(f"/api/session/{session_id}/groups", json={"group_id": "shoes", "name": "Обувь"})
    client.post(f"/api/session/{session_id}/groups", json={"group_id": "socks", "name": "Носки"})
    assert client.post(
        f"/api/session/{session_id}/assign",
        json={"entity_ids": ["ботинки зимние"], "group_id": "shoes"},
    ).status_code == 200
    assert client.post(
        f"/api/session/{session_id}/assign",
        json={"entity_ids": ["носки чёрные"], "group_id": "socks"},
    ).status_code == 200
    # "шапка вязаная" is deliberately left unassigned

    create_resp = client.post("/api/projects", json={"session_id": session_id, "name": "Отчёт WB"})
    assert create_resp.status_code == 201, create_resp.text
    project = create_resp.json()
    assert project["name"] == "Отчёт WB"
    assert project["groups_count"] == 2
    project_id = project["id"]

    # --- free-tier limit: a second project is a 409, not a silent 500/no-op
    second_session = _upload_and_finalize(client, FILE_A)
    limit_resp = client.post("/api/projects", json={"session_id": second_session, "name": "Второй"})
    assert limit_resp.status_code == 409
    assert "проект" in limit_resp.json()["detail"].lower()

    # --- listing / fetching
    list_resp = client.get("/api/projects")
    assert list_resp.status_code == 200
    assert [p["id"] for p in list_resp.json()] == [project_id]

    get_resp = client.get(f"/api/projects/{project_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["entity_column"] == "Товар"
    assert get_resp.json()["metric_column"] == "Сумма"

    assert client.get("/api/projects/00000000-0000-0000-0000-000000000000").status_code == 404

    # --- the actual point: re-upload a NEW file for the SAME project
    new_session_id = _upload_and_finalize(client, FILE_B)

    load_resp = client.post(f"/api/session/{new_session_id}/load-project/{project_id}")
    assert load_resp.status_code == 200, load_resp.text
    load_body = load_resp.json()
    assert load_body["known_entities_count"] == 2  # носки чёрные, ботинки зимние
    assert load_body["new_entities_count"] == 2
    assert sorted(load_body["new_entities"]) == ["трусы муж.", "шапка вязаная"]

    # --- entities list reflects is_new correctly post-load
    entities = {e["value"]: e for e in client.get(f"/api/session/{new_session_id}/entities").json()}
    assert entities["носки чёрные"]["is_new"] is False
    assert entities["ботинки зимние"]["is_new"] is False
    assert entities["шапка вязаная"]["is_new"] is True
    assert entities["трусы муж."]["is_new"] is True

    # --- known entities are already assigned to their old groups; the
    # reconciliation math already reflects that, no manual re-sort needed
    summary = client.get(f"/api/session/{new_session_id}/summary").json()
    assert Decimal(summary["rollup_totals"]["shoes"]) == Decimal("31000")
    assert Decimal(summary["rollup_totals"]["socks"]) == Decimal("5000")
    assert sorted(summary["unassigned_entities"]) == ["трусы муж.", "шапка вязаная"]
    assert Decimal(summary["unassigned_total"]) == Decimal("9200") + Decimal("3400")

    # --- the group tree itself carried over too (GET /groups, task #1)
    groups = {g["id"]: g for g in client.get(f"/api/session/{new_session_id}/groups").json()}
    assert set(groups) == {"shoes", "socks"}


def test_load_project_unknown_project_is_404(client):
    session_id = _upload_and_finalize(client, FILE_A)
    client.post("/api/auth/identify", json={"email": "x@example.com"})
    resp = client.post(f"/api/session/{session_id}/load-project/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404
