"""End-to-end HTTP tests for the FastAPI layer, run against a real .xlsx
file through fakeredis (no live Redis server, no live HTTP server — just
FastAPI's TestClient) — the same "prove it, don't trust the docs" standard
core/test_queues.py already set for this project.

Exercises the full real workflow: upload a file that has a decorative
title row before its real header (the exact case parse_file_raw() /
backend/columns.py exist to handle) -> pick header + columns -> list
unique entities -> build a nested group tree -> bulk-assign in several
separate calls (not one big call) -> confirm the reconciliation math
("Итого" = sum of every row, including "Не распределено") -> export and
read the actual .xlsx bytes back with openpyxl to check the numbers that
really landed in the file, not just what the API claimed. Also covers the
required failure paths: deleting a non-empty group, assigning into a
non-leaf group, and hitting an unknown session — each must come back as a
meaningful HTTP error, never a bare 500 or silent data loss.
"""

from decimal import Decimal
from io import BytesIO

import fakeredis
import openpyxl
import pytest
from fastapi.testclient import TestClient

from backend.main import app, get_redis


@pytest.fixture
def client():
    fake_redis = fakeredis.FakeStrictRedis()
    app.dependency_overrides[get_redis] = lambda: fake_redis
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _build_test_xlsx_bytes() -> bytes:
    """A real .xlsx (via openpyxl, not Polars' write_excel — which would
    inject its own extra header row and muddy exactly the scenario under
    test) shaped like a real 1C/WB export: a decorative report-title row
    sits above the real header."""
    wb = openpyxl.Workbook()
    ws = wb.active
    rows = [
        ["Отчёт по продажам — сентябрь 2026", "", ""],
        ["Товар", "Кол-во", "Сумма"],
        ["носки чёрные", "15", "4500"],
        ["носки чёрные", "10", "3000"],
        ["ботинки зимние", "4", "32000"],
        ["шапка вязаная", "10", "8900"],
        ["трусы муж.", "10", "3400"],
    ]
    for row in rows:
        ws.append(row)
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _upload(client) -> str:
    response = client.post(
        "/api/upload",
        files={
            "file": (
                "Продажи_сентябрь.xlsx",
                _build_test_xlsx_bytes(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_upload_returns_raw_preview_including_decorative_row(client):
    body = _upload(client)
    assert body["session_id"]
    assert body["columns_count"] == 3
    assert body["row_count"] == 7  # decorative + header + 5 data rows, nothing dropped
    assert body["preview_rows"][0][0] == "Отчёт по продажам — сентябрь 2026"
    assert body["preview_rows"][1] == ["Товар", "Кол-во", "Сумма"]


def test_full_workflow_reconciles_and_exports_correctly(client):
    upload_body = _upload(client)
    session_id = upload_body["session_id"]

    # --- columns: row 1 is the real header, col 0 = entity, col 2 = metric
    columns_resp = client.post(
        f"/api/session/{session_id}/columns",
        json={"header_row_index": 1, "entity_column": 0, "metric_column": 2},
    )
    assert columns_resp.status_code == 200, columns_resp.text
    columns_body = columns_resp.json()
    assert columns_body["entity_column"] == "Товар"
    assert columns_body["metric_column"] == "Сумма"
    assert columns_body["row_count"] == 5

    # --- entities: 4 unique values, "носки чёрные" appears twice
    entities_resp = client.get(f"/api/session/{session_id}/entities")
    assert entities_resp.status_code == 200
    entities = {e["value"]: e for e in entities_resp.json()}
    assert len(entities) == 4
    assert entities["носки чёрные"]["occurrences"] == 2
    assert entities["носки чёрные"]["is_new"] is True

    # --- build a nested tree: Одежда > {Носки, Бельё}, plus root Обувь
    def create_group(group_id, name, parent_id=None):
        resp = client.post(
            f"/api/session/{session_id}/groups",
            json={"group_id": group_id, "name": name, "parent_id": parent_id},
        )
        assert resp.status_code == 201, resp.text
        return resp

    create_group("clothes", "Одежда")
    create_group("socks", "Носки", "clothes")
    create_group("underwear", "Бельё", "clothes")
    create_group("shoes", "Обувь")

    # --- GET /groups: lets a client rebuild the tree after a hard reload
    # (the whole point of this endpoint — see backend/main.py get_groups)
    groups_resp = client.get(f"/api/session/{session_id}/groups")
    assert groups_resp.status_code == 200
    groups_by_id = {g["id"]: g for g in groups_resp.json()}
    assert set(groups_by_id) == {"clothes", "socks", "underwear", "shoes"}
    assert groups_by_id["socks"]["parent_id"] == "clothes"
    assert groups_by_id["socks"]["name"] == "Носки"
    assert groups_by_id["clothes"]["parent_id"] is None

    # duplicate group id must be rejected, not silently overwrite
    dup_resp = client.post(
        f"/api/session/{session_id}/groups",
        json={"group_id": "socks", "name": "Носки 2"},
    )
    assert dup_resp.status_code == 409

    # --- failure path: deleting a non-empty group (has children) is a 409
    delete_nonempty = client.delete(f"/api/session/{session_id}/groups/clothes")
    assert delete_nonempty.status_code == 409
    assert "clothes" in delete_nonempty.json()["detail"] or "не" in delete_nonempty.json()["detail"].lower()

    # --- failure path: assigning into a non-leaf group is a 409
    assign_non_leaf = client.post(
        f"/api/session/{session_id}/assign",
        json={"entity_ids": ["носки чёрные"], "group_id": "clothes"},
    )
    assert assign_non_leaf.status_code == 409

    # --- bulk assign in SEPARATE calls (not one big batch) — deliberately
    # leaves "шапка вязаная" unassigned to prove the "Не распределено"
    # bucket works
    r1 = client.post(
        f"/api/session/{session_id}/assign",
        json={"entity_ids": ["носки чёрные"], "group_id": "socks"},
    )
    assert r1.status_code == 200, r1.text

    r2 = client.post(
        f"/api/session/{session_id}/assign",
        json={"entity_ids": ["трусы муж."], "group_id": "underwear"},
    )
    assert r2.status_code == 200, r2.text

    r3 = client.post(
        f"/api/session/{session_id}/assign",
        json={"entity_ids": ["ботинки зимние"], "group_id": "shoes"},
    )
    assert r3.status_code == 200, r3.text

    # --- summary: math must reconcile exactly
    summary_resp = client.get(f"/api/session/{session_id}/summary")
    assert summary_resp.status_code == 200
    summary = summary_resp.json()

    assert Decimal(summary["rollup_totals"]["socks"]) == Decimal("7500")  # 4500 + 3000
    assert Decimal(summary["rollup_totals"]["underwear"]) == Decimal("3400")
    assert Decimal(summary["rollup_totals"]["clothes"]) == Decimal("10900")  # socks + underwear
    assert Decimal(summary["rollup_totals"]["shoes"]) == Decimal("32000")
    assert Decimal(summary["unassigned_total"]) == Decimal("8900")  # шапка вязаная
    assert summary["unassigned_entities"] == ["шапка вязаная"]

    file_total = Decimal("4500") + Decimal("3000") + Decimal("32000") + Decimal("8900") + Decimal("3400")
    assert file_total == Decimal("51800")
    assert Decimal(summary["grand_total"]) == file_total

    # --- export: download the real .xlsx and verify the actual bytes
    export_resp = client.get(f"/api/session/{session_id}/export")
    assert export_resp.status_code == 200
    assert export_resp.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

    workbook = openpyxl.load_workbook(BytesIO(export_resp.content))
    assert workbook.sheetnames == ["Детализация", "Итоги"]

    summary_sheet = workbook["Итоги"]
    summary_rows = {row[0].value.strip(): row[1].value for row in summary_sheet.iter_rows(min_row=2)}
    assert summary_rows["ИТОГО"] == "51800.0" or Decimal(summary_rows["ИТОГО"]) == file_total
    assert Decimal(summary_rows["Не распределено"]) == Decimal("8900")

    detail_sheet = workbook["Детализация"]
    detail_header = [c.value for c in detail_sheet[1]]
    assert "Группа 1" in detail_header
    assert "Группа 2" in detail_header

    detail_rows = list(detail_sheet.iter_rows(min_row=2, values_only=True))
    header_index = {name: i for i, name in enumerate(detail_header)}
    unassigned_row = next(r for r in detail_rows if r[header_index["Товар"]] == "шапка вязаная")
    assert unassigned_row[header_index["Группа 1"]] == "Не распределено"
    assigned_row = next(r for r in detail_rows if r[header_index["Товар"]] == "трусы муж.")
    assert assigned_row[header_index["Группа 1"]] == "Одежда"
    assert assigned_row[header_index["Группа 2"]] == "Бельё"

    # --- deleting an empty leaf group succeeds (204) and it's really gone
    delete_shoes_blocked = client.delete(f"/api/session/{session_id}/groups/shoes")
    assert delete_shoes_blocked.status_code == 409  # still has "ботинки зимние" assigned

    unassign_resp = client.post(
        f"/api/session/{session_id}/assign",
        json={"entity_ids": ["ботинки зимние"], "group_id": "socks"},
    )
    assert unassign_resp.status_code == 200  # re-assigning frees up "shoes"

    delete_shoes_ok = client.delete(f"/api/session/{session_id}/groups/shoes")
    assert delete_shoes_ok.status_code == 204

    entities_after_delete = client.get(f"/api/session/{session_id}/entities")
    assert entities_after_delete.status_code == 200  # session still usable afterwards


def test_assign_to_unknown_group_is_a_client_error_not_a_500(client):
    upload_body = _upload(client)
    session_id = upload_body["session_id"]
    client.post(
        f"/api/session/{session_id}/columns",
        json={"header_row_index": 1, "entity_column": 0, "metric_column": 2},
    )

    resp = client.post(
        f"/api/session/{session_id}/assign",
        json={"entity_ids": ["носки чёрные"], "group_id": "does-not-exist"},
    )
    assert resp.status_code == 400


def test_unknown_session_returns_404_everywhere(client):
    fake_id = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/api/session/{fake_id}/entities").status_code == 404
    assert client.get(f"/api/session/{fake_id}/summary").status_code == 404
    assert client.get(f"/api/session/{fake_id}/export").status_code == 404
    assert client.get(f"/api/session/{fake_id}/groups").status_code == 404
    assert (
        client.post(f"/api/session/{fake_id}/groups", json={"group_id": "g1", "name": "Тест"}).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/session/{fake_id}/assign", json={"entity_ids": ["x"], "group_id": "g1"}
        ).status_code
        == 404
    )
    assert client.delete(f"/api/session/{fake_id}/groups/g1").status_code == 404


def test_entities_before_columns_set_is_a_409_not_a_crash(client):
    upload_body = _upload(client)
    session_id = upload_body["session_id"]
    resp = client.get(f"/api/session/{session_id}/entities")
    assert resp.status_code == 409


def test_unsupported_file_type_is_rejected_cleanly(client):
    resp = client.post(
        "/api/upload",
        files={"file": ("report.pdf", b"%PDF-1.4 not really a spreadsheet", "application/pdf")},
    )
    assert resp.status_code == 400


def test_out_of_range_header_row_index_is_a_400(client):
    upload_body = _upload(client)
    session_id = upload_body["session_id"]
    resp = client.post(
        f"/api/session/{session_id}/columns",
        json={"header_row_index": 999, "entity_column": 0, "metric_column": 2},
    )
    assert resp.status_code == 400
