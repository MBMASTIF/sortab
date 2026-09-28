"""End-to-end HTTP tests for the FastAPI layer, run against real .xlsx
files through fakeredis (no live Redis server, no live HTTP server — just
FastAPI's TestClient) — the same "prove it, don't trust the docs" standard
core/test_queues.py already set for this project.

Covers the full real workflow, including a session that categorizes MORE
THAN ONE column at once (core/session.py's independent-trees phase — see
README): upload a file with a decorative title row before its real header
-> pick header + one or more columns to categorize (each gets its own
independent tree) + optional raw "разбивка" columns -> list unique
entities per categorized column -> build a nested group tree per column ->
bulk-assign -> confirm each tree's OWN reconciliation ("Итого" = sum of
every row, including "Не распределено", independently per tree) ->
export and read the actual .xlsx bytes back with openpyxl. Also covers the
required failure paths: deleting a non-empty group, assigning into a
non-leaf group, referencing an unknown categorized column, and hitting an
unknown session — each must come back as a meaningful HTTP error, never a
bare 500 or silent data loss.
"""

from decimal import Decimal
from io import BytesIO
from pathlib import Path

import fakeredis
import openpyxl
import pytest
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

    # --- columns: row 1 is the real header, col 0 = the ONE categorized
    # column ("Товар"), col 2 = metric. Single-categorized-column case —
    # the common path, and the one that must behave identically to before
    # this session gained the ability to categorize more than one column.
    columns_resp = client.post(
        f"/api/session/{session_id}/columns",
        json={"header_row_index": 1, "categorized_columns": [0], "metric_columns": [2]},
    )
    assert columns_resp.status_code == 200, columns_resp.text
    columns_body = columns_resp.json()
    assert columns_body["categorized_columns"] == ["Товар"]
    assert columns_body["metric_columns"] == ["Сумма"]
    assert columns_body["dimension_columns"] == []
    assert columns_body["row_count"] == 5

    # --- GET /columns: lets a client (app.html after redirect) rediscover
    # which columns this session categorizes, to build tabs.
    get_columns_resp = client.get(f"/api/session/{session_id}/columns")
    assert get_columns_resp.status_code == 200
    assert get_columns_resp.json()["categorized_columns"] == ["Товар"]

    # --- entities: 4 unique values, "носки чёрные" appears twice
    entities_resp = client.get(f"/api/session/{session_id}/entities/Товар")
    assert entities_resp.status_code == 200
    entities = {e["value"]: e for e in entities_resp.json()}
    assert len(entities) == 4
    assert entities["носки чёрные"]["occurrences"] == 2
    assert entities["носки чёрные"]["is_new"] is True

    # --- referencing a column that isn't categorized in this session is a 404
    assert client.get(f"/api/session/{session_id}/entities/Сумма").status_code == 404

    # --- build a nested tree: Одежда > {Носки, Бельё}, plus root Обувь
    def create_group(group_id, name, parent_id=None):
        resp = client.post(
            f"/api/session/{session_id}/groups/Товар",
            json={"group_id": group_id, "name": name, "parent_id": parent_id},
        )
        assert resp.status_code == 201, resp.text
        return resp

    create_group("clothes", "Одежда")
    create_group("socks", "Носки", "clothes")
    create_group("underwear", "Бельё", "clothes")
    create_group("shoes", "Обувь")

    # --- GET /groups/{column}: lets a client rebuild the tree after a hard
    # reload (the whole point of this endpoint — see backend/main.py get_groups)
    groups_resp = client.get(f"/api/session/{session_id}/groups/Товар")
    assert groups_resp.status_code == 200
    groups_by_id = {g["id"]: g for g in groups_resp.json()}
    assert set(groups_by_id) == {"clothes", "socks", "underwear", "shoes"}
    assert groups_by_id["socks"]["parent_id"] == "clothes"
    assert groups_by_id["socks"]["name"] == "Носки"
    assert groups_by_id["clothes"]["parent_id"] is None

    # duplicate group id must be rejected, not silently overwrite
    dup_resp = client.post(
        f"/api/session/{session_id}/groups/Товар",
        json={"group_id": "socks", "name": "Носки 2"},
    )
    assert dup_resp.status_code == 409

    # --- failure path: deleting a non-empty group (has children) is a 409
    delete_nonempty = client.delete(f"/api/session/{session_id}/groups/Товар/clothes")
    assert delete_nonempty.status_code == 409
    assert "clothes" in delete_nonempty.json()["detail"] or "не" in delete_nonempty.json()["detail"].lower()

    # --- failure path: assigning into a non-leaf group is a 409
    assign_non_leaf = client.post(
        f"/api/session/{session_id}/assign/Товар",
        json={"entity_ids": ["носки чёрные"], "group_id": "clothes"},
    )
    assert assign_non_leaf.status_code == 409

    # --- bulk assign in SEPARATE calls (not one big batch) — deliberately
    # leaves "шапка вязаная" unassigned to prove the "Не распределено"
    # bucket works
    r1 = client.post(
        f"/api/session/{session_id}/assign/Товар",
        json={"entity_ids": ["носки чёрные"], "group_id": "socks"},
    )
    assert r1.status_code == 200, r1.text

    r2 = client.post(
        f"/api/session/{session_id}/assign/Товар",
        json={"entity_ids": ["трусы муж."], "group_id": "underwear"},
    )
    assert r2.status_code == 200, r2.text

    r3 = client.post(
        f"/api/session/{session_id}/assign/Товар",
        json={"entity_ids": ["ботинки зимние"], "group_id": "shoes"},
    )
    assert r3.status_code == 200, r3.text

    # --- summary: math must reconcile exactly. Response shape (this
    # phase, see README/backend/main.py::get_summary): one summary PER
    # metric under "metrics", keyed by metric column name — this session
    # has exactly one sum column ("Сумма"), so there's exactly one key.
    summary_resp = client.get(f"/api/session/{session_id}/summary/Товар")
    assert summary_resp.status_code == 200
    summary_body = summary_resp.json()
    assert summary_body["metric_columns"] == ["Сумма"]
    summary = summary_body["metrics"]["Сумма"]

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
    # Single categorized column -> identical sheet names to the pre-multi-tree shape.
    assert workbook.sheetnames == ["Детализация", "Итоги"]

    summary_sheet = workbook["Итоги"]
    summary_rows = {row[0].value.strip(): row[1].value for row in summary_sheet.iter_rows(min_row=2)}
    assert summary_rows["ИТОГО"] == "51800.0" or Decimal(summary_rows["ИТОГО"]) == file_total
    assert Decimal(summary_rows["Не распределено"]) == Decimal("8900")

    detail_sheet = workbook["Детализация"]
    detail_header = [c.value for c in detail_sheet[1]]
    assert "Товар: Группа 1" in detail_header
    assert "Товар: Группа 2" in detail_header

    detail_rows = list(detail_sheet.iter_rows(min_row=2, values_only=True))
    header_index = {name: i for i, name in enumerate(detail_header)}
    unassigned_row = next(r for r in detail_rows if r[header_index["Товар"]] == "шапка вязаная")
    assert unassigned_row[header_index["Товар: Группа 1"]] == "Не распределено"
    assigned_row = next(r for r in detail_rows if r[header_index["Товар"]] == "трусы муж.")
    assert assigned_row[header_index["Товар: Группа 1"]] == "Одежда"
    assert assigned_row[header_index["Товар: Группа 2"]] == "Бельё"

    # --- deleting an empty leaf group succeeds (204) and it's really gone
    delete_shoes_blocked = client.delete(f"/api/session/{session_id}/groups/Товар/shoes")
    assert delete_shoes_blocked.status_code == 409  # still has "ботинки зимние" assigned

    unassign_resp = client.post(
        f"/api/session/{session_id}/assign/Товар",
        json={"entity_ids": ["ботинки зимние"], "group_id": "socks"},
    )
    assert unassign_resp.status_code == 200  # re-assigning frees up "shoes"

    delete_shoes_ok = client.delete(f"/api/session/{session_id}/groups/Товар/shoes")
    assert delete_shoes_ok.status_code == 204

    entities_after_delete = client.get(f"/api/session/{session_id}/entities/Товар")
    assert entities_after_delete.status_code == 200  # session still usable afterwards


def _synthetic_manager_client_product_xlsx() -> bytes:
    """Same STRUCTURE as a real 1C "Продажи" export used elsewhere in this
    project's fixtures (Менеджер / Клиент / Товар / Выручка columns, a few
    rows per manager+client combination) — but every name and figure here
    is invented for this test, not a real client's data."""
    wb = openpyxl.Workbook()
    ws = wb.active
    rows = [
        ["Менеджер", "Клиент", "Товар", "Выручка"],
        ["Смирнова А.", "ООО Ромашка", "носки чёрные", "4500"],
        ["Смирнова А.", "ООО Ромашка", "носки чёрные", "3000"],
        ["Смирнова А.", "ИП Кузнецов", "ботинки зимние", "32000"],
        ["Кузьмин Д.", "ООО Вектор", "шапка вязаная", "8900"],
        ["Кузьмин Д.", "ООО Вектор", "трусы муж.", "3400"],
        ["Кузьмин Д.", "ИП Кузнецов", "носки чёрные", "1200"],
    ]
    for row in rows:
        ws.append(row)
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def test_two_independent_trees_reconcile_separately_in_the_same_session(client):
    """The core claim of this phase: a session can categorize TWO columns
    at once (Товар AND Клиент), each gets its own independent tree, and
    each tree's "Итого = По группам + Не распределено" holds on its own —
    with a third column (Менеджер) carried through as raw context (no
    tree). All expected numbers are computed independently below, not
    trusted from the implementation."""
    upload_resp = client.post(
        "/api/upload",
        files={
            "file": (
                "Продажи.xlsx",
                _synthetic_manager_client_product_xlsx(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert upload_resp.status_code == 200, upload_resp.text
    session_id = upload_resp.json()["session_id"]

    # Товар = col 2, Клиент = col 1 -> both categorized; Выручка = col 3;
    # Менеджер = col 0 -> raw разбивка, no tree.
    columns_resp = client.post(
        f"/api/session/{session_id}/columns",
        json={
            "header_row_index": 0,
            "categorized_columns": [2, 1],
            "metric_columns": [3],
            "dimension_columns": [0],
        },
    )
    assert columns_resp.status_code == 200, columns_resp.text
    body = columns_resp.json()
    assert body["categorized_columns"] == ["Товар", "Клиент"]
    assert body["metric_columns"] == ["Выручка"]
    assert body["dimension_columns"] == ["Менеджер"]

    file_total = Decimal("4500") + Decimal("3000") + Decimal("32000") + Decimal("8900") + Decimal("3400") + Decimal("1200")
    assert file_total == Decimal("53000")

    # --- Товар tree: fully categorized into 2 leaf groups
    client.post(f"/api/session/{session_id}/groups/Товар", json={"group_id": "socks", "name": "Носки"})
    client.post(f"/api/session/{session_id}/groups/Товар", json={"group_id": "other", "name": "Остальное"})
    assert client.post(
        f"/api/session/{session_id}/assign/Товар",
        json={"entity_ids": ["носки чёрные"], "group_id": "socks"},
    ).status_code == 200
    assert client.post(
        f"/api/session/{session_id}/assign/Товар",
        json={"entity_ids": ["ботинки зимние", "шапка вязаная", "трусы муж."], "group_id": "other"},
    ).status_code == 200

    tovar_summary = client.get(f"/api/session/{session_id}/summary/Товар").json()["metrics"]["Выручка"]
    # носки чёрные rows: 4500 + 3000 + 1200 = 8700
    assert Decimal(tovar_summary["rollup_totals"]["socks"]) == Decimal("8700")
    assert Decimal(tovar_summary["rollup_totals"]["other"]) == Decimal("32000") + Decimal("8900") + Decimal("3400")
    assert Decimal(tovar_summary["unassigned_total"]) == Decimal("0")
    assert Decimal(tovar_summary["grand_total"]) == file_total  # reconciles independently

    # --- Клиент tree: deliberately left with one client unassigned, to
    # prove ITS OWN "Не распределено" bucket works independently of Товар's.
    client.post(f"/api/session/{session_id}/groups/Клиент", json={"group_id": "big", "name": "Крупные"})
    assert client.post(
        f"/api/session/{session_id}/assign/Клиент",
        json={"entity_ids": ["ООО Ромашка", "ООО Вектор"], "group_id": "big"},
    ).status_code == 200
    # "ИП Кузнецов" (32000 + 1200 = 33200) deliberately left unassigned

    client_summary = client.get(f"/api/session/{session_id}/summary/Клиент").json()["metrics"]["Выручка"]
    assert Decimal(client_summary["rollup_totals"]["big"]) == Decimal("4500") + Decimal("3000") + Decimal("8900") + Decimal("3400")
    assert Decimal(client_summary["unassigned_total"]) == Decimal("32000") + Decimal("1200")
    assert client_summary["unassigned_entities"] == ["ИП Кузнецов"]
    assert Decimal(client_summary["grand_total"]) == file_total  # independently reconciles too

    # --- a group id from the Товар tree must not be usable in the Клиент
    # tree's namespace — separate TreeStores, not a shared id space.
    cross_assign = client.post(
        f"/api/session/{session_id}/assign/Клиент",
        json={"entity_ids": ["ИП Кузнецов"], "group_id": "socks"},
    )
    assert cross_assign.status_code == 400

    # --- Разбивка: Менеджер x Товар-tree path
    breakdown_resp = client.get(f"/api/session/{session_id}/breakdown/Товар")
    assert breakdown_resp.status_code == 200
    breakdown_rows = breakdown_resp.json()["rows"]
    by_key = {(r["Менеджер"], r["Группа 1"]): Decimal(r["Выручка"]) for r in breakdown_rows}
    # Смирнова А. носки чёрные (socks): 4500 + 3000 = 7500
    assert by_key[("Смирнова А.", "Носки")] == Decimal("7500")
    # Кузьмин Д. носки чёрные (socks): 1200
    assert by_key[("Кузьмин Д.", "Носки")] == Decimal("1200")
    # Смирнова А. ботинки зимние (other): 32000
    assert by_key[("Смирнова А.", "Остальное")] == Decimal("32000")
    # Кузьмин Д. шапка + трусы (other): 8900 + 3400 = 12300
    assert by_key[("Кузьмин Д.", "Остальное")] == Decimal("12300")

    # --- export: both trees' "Итоги" sheets present, plus both breakdowns,
    # plus a detail sheet with a path per tree
    export_resp = client.get(f"/api/session/{session_id}/export")
    assert export_resp.status_code == 200
    workbook = openpyxl.load_workbook(BytesIO(export_resp.content))
    assert set(workbook.sheetnames) == {
        "Детализация", "Итоги", "Итоги — Клиент", "Разбивка", "Разбивка — Клиент",
    }

    detail_header = [c.value for c in workbook["Детализация"][1]]
    assert "Товар: Группа 1" in detail_header
    assert "Клиент: Группа 1" in detail_header
    assert detail_header[0] == "Менеджер"  # dimension column pulled to front

    itogi_sheet = workbook["Итоги"]
    itogi_rows = {row[0].value.strip(): row[1].value for row in itogi_sheet.iter_rows(min_row=2)}
    assert Decimal(itogi_rows["ИТОГО"]) == file_total

    client_itogi_sheet = workbook["Итоги — Клиент"]
    client_itogi_rows = {row[0].value.strip(): row[1].value for row in client_itogi_sheet.iter_rows(min_row=2)}
    assert Decimal(client_itogi_rows["ИТОГО"]) == file_total
    assert Decimal(client_itogi_rows["Не распределено"]) == Decimal("32000") + Decimal("1200")


def test_full_workflow_with_pdf_source_reconciles_correctly(client):
    """The architecture claim this whole feature exists to prove: a PDF
    source goes through upload -> columns -> entities -> groups -> assign
    -> summary -> export using EXACTLY the same endpoints as the Excel
    workflow above, no PDF-specific branching anywhere in this file.
    Uses the multi-page fixture (3 pages, repeated header, 12 data rows)
    so this also proves core.parsing._glue_pdf_page_grids()'s output is
    directly usable by backend/columns.py::finalize_pipeline_columns()
    unmodified. Expected total (79390.55) is the same independently
    Decimal-computed number backend/tests/fixtures/generate_fixtures.py
    prints when the fixture is generated — not trusted blindly here."""
    pdf_bytes = (FIXTURES_DIR / "pdf_multipage_table.pdf").read_bytes()

    upload_resp = client.post(
        "/api/upload",
        files={"file": ("Накладная.pdf", pdf_bytes, "application/pdf")},
    )
    assert upload_resp.status_code == 200, upload_resp.text
    upload_body = upload_resp.json()
    session_id = upload_body["session_id"]
    # header glued once at row 0, 12 data rows, duplicate per-page headers
    # already dropped by _glue_pdf_page_grids() before this ever reaches
    # the API layer.
    assert upload_body["row_count"] == 13
    assert upload_body["preview_rows"][0] == ["Товар", "Кол-во", "Сумма"]

    columns_resp = client.post(
        f"/api/session/{session_id}/columns",
        json={"header_row_index": 0, "categorized_columns": [0], "metric_columns": [2]},
    )
    assert columns_resp.status_code == 200, columns_resp.text
    assert columns_resp.json()["row_count"] == 12

    entities_resp = client.get(f"/api/session/{session_id}/entities/Товар")
    assert entities_resp.status_code == 200
    entities = entities_resp.json()
    assert len(entities) == 12  # every product name in the fixture is unique

    create_resp = client.post(
        f"/api/session/{session_id}/groups/Товар",
        json={"group_id": "all", "name": "Всё"},
    )
    assert create_resp.status_code == 201, create_resp.text

    assign_resp = client.post(
        f"/api/session/{session_id}/assign/Товар",
        json={"entity_ids": [e["value"] for e in entities], "group_id": "all"},
    )
    assert assign_resp.status_code == 200, assign_resp.text
    assert assign_resp.json()["assigned"] == 12

    summary_resp = client.get(f"/api/session/{session_id}/summary/Товар")
    assert summary_resp.status_code == 200
    summary = summary_resp.json()["metrics"]["Сумма"]
    assert Decimal(summary["unassigned_total"]) == Decimal("0")
    assert Decimal(summary["rollup_totals"]["all"]) == Decimal("79390.55")
    assert Decimal(summary["grand_total"]) == Decimal("79390.55")

    export_resp = client.get(f"/api/session/{session_id}/export")
    assert export_resp.status_code == 200
    workbook = openpyxl.load_workbook(BytesIO(export_resp.content))
    summary_sheet = workbook["Итоги"]
    summary_rows = {row[0].value.strip(): row[1].value for row in summary_sheet.iter_rows(min_row=2)}
    assert Decimal(summary_rows["ИТОГО"]) == Decimal("79390.55")


def test_assign_to_unknown_group_is_a_client_error_not_a_500(client):
    upload_body = _upload(client)
    session_id = upload_body["session_id"]
    client.post(
        f"/api/session/{session_id}/columns",
        json={"header_row_index": 1, "categorized_columns": [0], "metric_columns": [2]},
    )

    resp = client.post(
        f"/api/session/{session_id}/assign/Товар",
        json={"entity_ids": ["носки чёрные"], "group_id": "does-not-exist"},
    )
    assert resp.status_code == 400


def test_unknown_session_returns_404_everywhere(client):
    fake_id = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/api/session/{fake_id}/entities/Товар").status_code == 404
    assert client.get(f"/api/session/{fake_id}/summary/Товар").status_code == 404
    assert client.get(f"/api/session/{fake_id}/export").status_code == 404
    assert client.get(f"/api/session/{fake_id}/groups/Товар").status_code == 404
    assert (
        client.post(f"/api/session/{fake_id}/groups/Товар", json={"group_id": "g1", "name": "Тест"}).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/session/{fake_id}/assign/Товар", json={"entity_ids": ["x"], "group_id": "g1"}
        ).status_code
        == 404
    )
    assert client.delete(f"/api/session/{fake_id}/groups/Товар/g1").status_code == 404


def test_entities_before_columns_set_is_a_409_not_a_crash(client):
    upload_body = _upload(client)
    session_id = upload_body["session_id"]
    resp = client.get(f"/api/session/{session_id}/entities/Товар")
    assert resp.status_code == 409


def test_unsupported_file_type_is_rejected_cleanly(client):
    # NOT .pdf any more: PDF is now a supported input format (camelot,
    # flavor="stream" — see core/parsing.py and tests/test_parsing_pdf.py).
    # .docx is still genuinely unsupported.
    resp = client.post(
        "/api/upload",
        files={"file": ("report.docx", b"not really a spreadsheet", "application/octet-stream")},
    )
    assert resp.status_code == 400


def test_malformed_pdf_is_rejected_cleanly_not_500(client):
    """A .pdf upload whose bytes aren't a real PDF at all must still come
    back as a clean 400, not an unhandled 500 from camelot/pdfminer
    choking on garbage input."""
    resp = client.post(
        "/api/upload",
        files={"file": ("report.pdf", b"%PDF-1.4 not really a valid pdf body", "application/pdf")},
    )
    assert resp.status_code == 400


def test_out_of_range_header_row_index_is_a_400(client):
    upload_body = _upload(client)
    session_id = upload_body["session_id"]
    resp = client.post(
        f"/api/session/{session_id}/columns",
        json={"header_row_index": 999, "categorized_columns": [0], "metric_columns": [2]},
    )
    assert resp.status_code == 400


def test_empty_categorized_columns_is_a_400(client):
    upload_body = _upload(client)
    session_id = upload_body["session_id"]
    resp = client.post(
        f"/api/session/{session_id}/columns",
        json={"header_row_index": 1, "categorized_columns": [], "metric_columns": [2]},
    )
    assert resp.status_code == 400


def test_dimension_column_overlapping_categorized_is_a_400(client):
    upload_body = _upload(client)
    session_id = upload_body["session_id"]
    resp = client.post(
        f"/api/session/{session_id}/columns",
        json={"header_row_index": 1, "categorized_columns": [0], "metric_columns": [2], "dimension_columns": [0]},
    )
    assert resp.status_code == 400
