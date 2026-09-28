"""End-to-end HTTP proof that a hierarchical 1C "expanded group tree"
report (see core/hierarchical.py) is auto-unflattened by POST /api/upload
and then flows through the EXISTING pipeline (columns -> entities -> tree
-> assign -> summary) completely unchanged — the whole point of shaping
detect_and_unflatten()'s output exactly like core.parsing.parse_file_raw().
All names/numbers are invented test data.
"""

from decimal import Decimal
from io import BytesIO
from pathlib import Path

import fakeredis
import pytest
import xlsxwriter
from fastapi.testclient import TestClient

from backend.main import app, get_redis

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def client():
    fake_redis = fakeredis.FakeStrictRedis()
    app.dependency_overrides[get_redis] = lambda: fake_redis
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# manager, client, point, item, Стоимость, Количество
_LEAVES = [
    ("Смирнова Анна", "ООО Ромашка", "Точка на Ленина", "Хлеб бородинский", 100.50, 10),
    ("Смирнова Анна", "ООО Ромашка", "Точка на Ленина", "Батон нарезной", 200.25, 20),
    ("Смирнова Анна", "ИП Кузнецов", "Точка склад", "Сахар", 300.00, 15),
    ("Петров Игорь", "ЗАО Вектор", "Точка офис", "Мука пшеничная", 150.00, 12),
]
_TOTAL_COST = 750.75
_TOTAL_QTY = 57


def _build_hierarchical_xlsx_bytes() -> bytes:
    buffer = BytesIO()
    wb = xlsxwriter.Workbook(buffer, {"in_memory": True})
    ws = wb.add_worksheet()
    fmt = {indent: wb.add_format({"indent": indent}) for indent in (0, 2, 4, 6)}

    r = 0
    ws.write(r, 1, "Стоимость")
    ws.write(r, 2, "Количество")
    r += 1
    ws.write(r, 0, "Итого", fmt[0])
    ws.write(r, 1, _TOTAL_COST)
    ws.write(r, 2, _TOTAL_QTY)
    r += 1
    for text, indent in [
        ("Торговый агент", 0), ("Контрагент", 2), ("Торговая точка", 4), ("Номенклатура", 6),
    ]:
        ws.write(r, 0, text, fmt[indent])
        r += 1

    current_manager = current_client = current_point = None
    for manager, client_name, point, item, cost, qty in _LEAVES:
        if manager != current_manager:
            ws.write(r, 0, manager, fmt[0])
            r += 1
            current_manager, current_client, current_point = manager, None, None
        if client_name != current_client:
            ws.write(r, 0, client_name, fmt[2])
            r += 1
            current_client, current_point = client_name, None
        if point != current_point:
            ws.write(r, 0, point, fmt[4])
            r += 1
            current_point = point
        ws.write(r, 0, item, fmt[6])
        ws.write(r, 1, cost)
        ws.write(r, 2, qty)
        r += 1

    wb.close()
    return buffer.getvalue()


def test_upload_auto_unflattens_hierarchical_report_with_real_column_names(client):
    response = client.post(
        "/api/upload",
        files={
            "file": (
                "Отчет_по_менеджерам.xlsx",
                _build_hierarchical_xlsx_bytes(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()

    # One row per LEAF (4), plus the header row promote_header_row() will
    # consume at header_row_index=0 — never generic "column_N" text.
    assert body["row_count"] == 5
    header = body["preview_rows"][0]
    assert header == ["Торговый агент", "Контрагент", "Торговая точка", "Номенклатура", "Стоимость", "Количество"]
    session_id = body["session_id"]

    columns_response = client.post(
        f"/api/session/{session_id}/columns",
        json={
            "header_row_index": 0,
            "categorized_columns": [0, 1, 2, 3],  # Менеджер, Клиент, Точка, Товар — 4 independent trees
            "metric_columns": [4],  # Стоимость
            "dimension_columns": [],
        },
    )
    assert columns_response.status_code == 200, columns_response.text
    cols_body = columns_response.json()
    assert cols_body["categorized_columns"] == [
        "Торговый агент", "Контрагент", "Торговая точка", "Номенклатура",
    ]
    assert cols_body["metric_columns"] == ["Стоимость"]
    assert cols_body["row_count"] == 4

    entities_response = client.get(f"/api/session/{session_id}/entities/Торговый агент")
    assert entities_response.status_code == 200
    entity_values = {e["value"] for e in entities_response.json()}
    assert entity_values == {"Смирнова Анна", "Петров Игорь"}

    # No categorization done yet — the reconciliation on any tree must
    # show everything as "Не распределено", summing to the Итого row's
    # own value (750.75), proving the leaf-level unflatten really did
    # carry the correct per-row Стоимость through to the real pipeline.
    summary_response = client.get(f"/api/session/{session_id}/summary/Номенклатура")
    assert summary_response.status_code == 200
    summary = summary_response.json()["metrics"]["Стоимость"]
    assert Decimal(summary["unassigned_total"]) == Decimal("750.75")
    assert Decimal(summary["grand_total"]) == Decimal("750.75")
