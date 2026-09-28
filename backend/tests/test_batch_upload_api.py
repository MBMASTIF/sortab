"""End-to-end HTTP tests for the multi-source upload flow — POST
/api/upload/batch + POST /api/upload/batch/{id}/consolidate (see README
"Задача: консолидация" and backend/consolidate.py's module docstring).

Covers the full real scenario: upload two monthly report files with the
same column structure but different decorative-preamble lengths -> pick
each source's header row -> consolidate -> the resulting session_id feeds
the EXACT SAME /api/session/{id}/columns endpoint a normal single-file
upload already uses -> category tree on "Товар" + "Источник" as a
разбивка column (= comparison by period, per README's stated design) ->
сверка reconciles to a hand-computed total. Also covers the required
failure paths: incompatible sources, unknown/expired batch, and a
multi-sheet Excel file auto-expanding into one source per sheet.
"""

from decimal import Decimal
from io import BytesIO

import fakeredis
import openpyxl
import pytest
import xlsxwriter
from fastapi.testclient import TestClient

from backend.main import app, get_redis


@pytest.fixture
def client():
    fake_redis = fakeredis.FakeStrictRedis()
    app.dependency_overrides[get_redis] = lambda: fake_redis
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _xlsx_bytes(rows: list[list]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _multi_sheet_xlsx_bytes(sheets: dict[str, list[list]]) -> bytes:
    buffer = BytesIO()
    wb = xlsxwriter.Workbook(buffer, {"in_memory": True})
    for name, rows in sheets.items():
        ws = wb.add_worksheet(name)
        for row_idx, row in enumerate(rows):
            ws.write_row(row_idx, 0, row)
    wb.close()
    buffer.seek(0)
    return buffer.getvalue()


def test_batch_upload_returns_one_source_per_file(client):
    june = _xlsx_bytes([["Товар", "Клиент", "Выручка"], ["Молоко", "ООО Ромашка", "1000"]])
    july = _xlsx_bytes([["Товар", "Клиент", "Выручка"], ["Хлеб", "ИП Иванов", "300"]])

    resp = client.post(
        "/api/upload/batch",
        files=[
            ("files", ("june.xlsx", june, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
            ("files", ("july.xlsx", july, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "batch_id" in body
    assert len(body["sources"]) == 2
    assert body["sources"][0]["filename"] == "june.xlsx"
    assert body["sources"][1]["filename"] == "july.xlsx"
    assert body["sources"][0]["preview_rows"][0] == ["Товар", "Клиент", "Выручка"]


def test_full_consolidation_round_trip_reconciles_by_source(client):
    """The real scenario from the task brief: two monthly reports, one
    with a decorative row before its header, consolidated into a session,
    categorized by "Товар", with "Источник" as a разбивка column (=
    comparison by period) -- сверка must equal a hand-computed total."""
    june = _xlsx_bytes(
        [
            ["Отчёт за Июнь 2026", "", ""],
            ["Товар", "Клиент", "Выручка"],
            ["Молоко", "ООО Ромашка", "1234,56"],
            ["Хлеб", "ИП Иванов", "500"],
        ]
    )
    july = _xlsx_bytes(
        [
            ["Товар", "Клиент", "Выручка"],
            ["Молоко", "ООО Ромашка", "1000"],
            ["Сыр", "ИП Петров", "2000"],
            ["Хлеб", "ИП Иванов", "300"],
        ]
    )

    upload_resp = client.post(
        "/api/upload/batch",
        files=[
            ("files", ("june.xlsx", june, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
            ("files", ("july.xlsx", july, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
        ],
    )
    assert upload_resp.status_code == 200
    upload_body = upload_resp.json()
    batch_id = upload_body["batch_id"]
    # June has a decorative row (header at index 1), July doesn't (index 0).
    assert upload_body["sources"][0]["preview_rows"][0][0] == "Отчёт за Июнь 2026"
    assert upload_body["sources"][1]["preview_rows"][0][0] == "Товар"

    consolidate_resp = client.post(
        f"/api/upload/batch/{batch_id}/consolidate",
        json={
            "sources": [
                {"index": 0, "header_row_index": 1, "label": "Июнь"},
                {"index": 1, "header_row_index": 0, "label": "Июль"},
            ]
        },
    )
    assert consolidate_resp.status_code == 200
    consolidate_body = consolidate_resp.json()
    session_id = consolidate_body["session_id"]
    assert consolidate_body["row_count"] == 6  # synthetic header row + 5 real data rows
    assert consolidate_body["preview_rows"][0] == ["Товар", "Клиент", "Выручка", "Источник"]

    # Batch is one-shot: re-consolidating the same (now-deleted) batch 404s.
    repeat_resp = client.post(
        f"/api/upload/batch/{batch_id}/consolidate",
        json={"sources": [{"index": 0, "header_row_index": 1, "label": "Июнь"}]},
    )
    assert repeat_resp.status_code == 404

    # From here on, this is the EXACT SAME flow a normal single-file
    # upload already uses.
    columns_resp = client.post(
        f"/api/session/{session_id}/columns",
        json={
            "header_row_index": 0,
            "categorized_columns": [0],  # Товар
            "metric_columns": [2],  # Выручка
            "dimension_columns": [3],  # Источник
        },
    )
    assert columns_resp.status_code == 200

    entities_resp = client.get(f"/api/session/{session_id}/entities/Товар")
    assert entities_resp.status_code == 200
    entities = entities_resp.json()
    entity_values = {e["value"] for e in entities}
    assert entity_values == {"Молоко", "Хлеб", "Сыр"}

    group_resp = client.post(
        f"/api/session/{session_id}/groups/Товар",
        json={"group_id": "g1", "name": "Молочка", "parent_id": None},
    )
    assert group_resp.status_code == 201

    molok_id = next(e["value"] for e in entities if e["value"] == "Молоко")
    assign_resp = client.post(
        f"/api/session/{session_id}/assign/Товар",
        json={"entity_ids": ["Молоко"], "group_id": "g1"},
    )
    assert assign_resp.status_code == 200

    summary_resp = client.get(f"/api/session/{session_id}/summary/Товар")
    assert summary_resp.status_code == 200
    summary = summary_resp.json()["metrics"]["Выручка"]
    # Молоко = 1234.56 (июнь) + 1000 (июль) = 2234.56
    assert Decimal(summary["rollup_totals"]["g1"]) == Decimal("1234.56") + Decimal("1000")
    total = Decimal("1234.56") + Decimal("500") + Decimal("1000") + Decimal("2000") + Decimal("300")
    assert Decimal(summary["grand_total"]) == total

    breakdown_resp = client.get(f"/api/session/{session_id}/breakdown/Товар")
    assert breakdown_resp.status_code == 200
    breakdown_rows = breakdown_resp.json()["rows"]
    june_rows = [r for r in breakdown_rows if r["Источник"] == "Июнь"]
    july_rows = [r for r in breakdown_rows if r["Источник"] == "Июль"]
    june_total = sum(Decimal(str(r["Выручка"])) for r in june_rows)
    july_total = sum(Decimal(str(r["Выручка"])) for r in july_rows)
    assert june_total == Decimal("1234.56") + Decimal("500")
    assert july_total == Decimal("1000") + Decimal("2000") + Decimal("300")


def test_consolidate_can_drop_a_source_by_omitting_its_index(client):
    a = _xlsx_bytes([["Товар", "Выручка"], ["Молоко", "100"]])
    b = _xlsx_bytes([["Товар", "Выручка"], ["Хлеб", "200"]])
    c = _xlsx_bytes([["Другое", "Совсем другое"], ["x", "y"]])  # incompatible, will be dropped

    upload_resp = client.post(
        "/api/upload/batch",
        files=[
            ("files", ("a.xlsx", a, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
            ("files", ("b.xlsx", b, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
            ("files", ("c.xlsx", c, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
        ],
    )
    batch_id = upload_resp.json()["batch_id"]

    consolidate_resp = client.post(
        f"/api/upload/batch/{batch_id}/consolidate",
        json={
            "sources": [
                {"index": 0, "header_row_index": 0, "label": "A"},
                {"index": 1, "header_row_index": 0, "label": "B"},
            ]
        },
    )
    assert consolidate_resp.status_code == 200
    assert consolidate_resp.json()["row_count"] == 3  # header + 2 data rows, "c" excluded


def test_batch_upload_expands_multi_sheet_excel_into_one_source_per_sheet(client):
    contents = _multi_sheet_xlsx_bytes(
        {
            "Июнь": [["Товар", "Выручка"], ["Молоко", "100"]],
            "Июль": [["Товар", "Выручка"], ["Хлеб", "200"]],
        }
    )

    upload_resp = client.post(
        "/api/upload/batch",
        files=[
            ("files", ("report.xlsx", contents, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
        ],
    )
    assert upload_resp.status_code == 200
    body = upload_resp.json()
    assert len(body["sources"]) == 2
    assert body["sources"][0]["filename"] == "report.xlsx — Июнь"
    assert body["sources"][1]["filename"] == "report.xlsx — Июль"


def test_consolidate_incompatible_sources_returns_clear_russian_error(client):
    a = _xlsx_bytes([["Товар", "Клиент", "Выручка"], ["Молоко", "ООО Ромашка", "100"]])
    b = _xlsx_bytes([["Товар", "Выручка"], ["Хлеб", "200"]])  # missing "Клиент"

    upload_resp = client.post(
        "/api/upload/batch",
        files=[
            ("files", ("a.xlsx", a, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
            ("files", ("b.xlsx", b, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
        ],
    )
    batch_id = upload_resp.json()["batch_id"]

    consolidate_resp = client.post(
        f"/api/upload/batch/{batch_id}/consolidate",
        json={
            "sources": [
                {"index": 0, "header_row_index": 0, "label": "Файл A"},
                {"index": 1, "header_row_index": 0, "label": "Файл B"},
            ]
        },
    )
    assert consolidate_resp.status_code == 400
    detail = consolidate_resp.json()["detail"]
    assert "Файл B" in detail
    # Must be Russian, not a bare English exception string.
    assert "колон" in detail.lower()


def test_consolidate_unknown_batch_id_returns_404(client):
    resp = client.post(
        "/api/upload/batch/does-not-exist/consolidate",
        json={"sources": [{"index": 0, "header_row_index": 0, "label": "X"}]},
    )
    assert resp.status_code == 404


def test_batch_upload_rejects_unsupported_file_with_russian_error(client):
    resp = client.post(
        "/api/upload/batch",
        files=[("files", ("notes.docx", b"not a real docx", "application/octet-stream"))],
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "формат" in detail.lower()


def test_batch_upload_empty_file_list_rejected(client):
    resp = client.post("/api/upload/batch", files=[])
    assert resp.status_code in (400, 422)
