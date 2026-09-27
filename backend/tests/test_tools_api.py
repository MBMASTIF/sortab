"""End-to-end HTTP tests for the Инструменты endpoints (Unpivot, Compare) —
see backend/main.py's "Инструменты" section and backend/tools_store.py.
Deliberately does NOT touch Redis or Postgres: these tools are stateless
and anonymous by design, so the only override needed is a fresh
ToolFileStore per test (no shared state leaking between tests, same
"override the dependency" pattern test_api.py uses for Redis).
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
    # tools_store.ToolFileStore is now Redis-backed (see its module
    # docstring for why: the live deploy runs multiple uvicorn workers, so
    # an in-process dict silently lost tokens across workers). Overriding
    # get_redis alone is enough — get_tools_store() in main.py builds its
    # ToolFileStore from this same connection, same pattern test_api.py
    # already uses for the Project pipeline's Redis-backed sessions.
    fake_redis = fakeredis.FakeStrictRedis()
    app.dependency_overrides[get_redis] = lambda: fake_redis
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _xlsx_bytes(rows: list[list]) -> bytes:
    import openpyxl as _openpyxl

    wb = _openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


# ---------------------------- Unpivot ----------------------------


def _unpivot_source_bytes() -> bytes:
    return _xlsx_bytes(
        [
            ["Товар", "Январь", "Февраль", "Март"],
            ["Носки", 1000, 1100, 1200],
            ["Ботинки", 2000, 2200, 2300],
        ]
    )


def test_unpivot_preview_returns_token_and_grid(client):
    resp = client.post(
        "/api/tools/preview",
        files={"file": ("Сводная.xlsx", _unpivot_source_bytes(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token"]
    assert body["row_count"] == 3
    assert body["preview_rows"][0] == ["Товар", "Январь", "Февраль", "Март"]


def test_unpivot_commit_downloads_long_format_xlsx(client):
    preview = client.post(
        "/api/tools/preview",
        files={"file": ("Сводная.xlsx", _unpivot_source_bytes(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    ).json()

    resp = client.post(
        "/api/tools/unpivot",
        json={
            "token": preview["token"],
            "header_row_index": 0,
            "id_columns": [0],
            "value_columns": [1, 2, 3],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

    wb = openpyxl.load_workbook(BytesIO(resp.content))
    ws = wb["Развёрнуто"]
    header = [c.value for c in ws[1]]
    assert header == ["Товар", "Категория", "Значение"]

    data_rows = list(ws.iter_rows(min_row=2, values_only=True))
    assert len(data_rows) == 6  # 2 products x 3 months

    total = sum(Decimal(str(r[2])) for r in data_rows)
    expected = Decimal(1000 + 1100 + 1200 + 2000 + 2200 + 2300)
    assert total == expected


def test_unpivot_rejects_fewer_than_two_value_columns(client):
    preview = client.post(
        "/api/tools/preview",
        files={"file": ("Сводная.xlsx", _unpivot_source_bytes(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    ).json()

    resp = client.post(
        "/api/tools/unpivot",
        json={"token": preview["token"], "header_row_index": 0, "id_columns": [0], "value_columns": [1]},
    )
    assert resp.status_code == 400


def test_unpivot_unknown_token_is_404(client):
    resp = client.post(
        "/api/tools/unpivot",
        json={"token": "does-not-exist", "header_row_index": 0, "id_columns": [0], "value_columns": [1, 2]},
    )
    assert resp.status_code == 404


# ---------------------------- Compare ----------------------------


def _compare_file_a_bytes() -> bytes:
    return _xlsx_bytes(
        [
            ["Контрагент", "Сумма"],
            ["ООО Ромашка", 10000],
            ["ИП Иванов", 5000],
            ["ЗАО Вектор", 777.77],
        ]
    )


def _compare_file_b_bytes() -> bytes:
    return _xlsx_bytes(
        [
            ["Контрагент", "Сумма"],
            ["ООО Ромашка", 10000],
            ["ИП Иванов", 5500],
            ["ООО Заря", 300],
        ]
    )


def _upload_for_compare(client, content: bytes, name: str) -> dict:
    resp = client.post(
        "/api/tools/preview",
        files={"file": (name, content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_compare_returns_all_four_statuses(client):
    a = _upload_for_compare(client, _compare_file_a_bytes(), "A.xlsx")
    b = _upload_for_compare(client, _compare_file_b_bytes(), "B.xlsx")

    resp = client.post(
        "/api/tools/compare",
        json={
            "token_a": a["token"],
            "header_row_index_a": 0,
            "entity_column_a": 0,
            "metric_column_a": 1,
            "token_b": b["token"],
            "header_row_index_b": 0,
            "entity_column_b": 0,
            "metric_column_b": 1,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    by_entity = {r["entity"]: r for r in body["rows"]}
    assert by_entity["ООО Ромашка"]["status"] == "совпадает"
    assert Decimal(by_entity["ООО Ромашка"]["diff"]) == Decimal("0")

    assert by_entity["ИП Иванов"]["status"] == "расхождение"
    assert Decimal(by_entity["ИП Иванов"]["diff"]) == Decimal("500")

    assert by_entity["ЗАО Вектор"]["status"] == "только в A"
    assert by_entity["ЗАО Вектор"]["sum_b"] is None

    assert by_entity["ООО Заря"]["status"] == "только в B"
    assert by_entity["ООО Заря"]["sum_a"] is None

    assert body["summary"]["matched"] == 1
    assert body["summary"]["mismatched"] == 1
    assert body["summary"]["only_a"] == 1
    assert body["summary"]["only_b"] == 1


def test_compare_export_downloads_xlsx_with_same_rows(client):
    a = _upload_for_compare(client, _compare_file_a_bytes(), "A.xlsx")
    b = _upload_for_compare(client, _compare_file_b_bytes(), "B.xlsx")

    body = {
        "token_a": a["token"],
        "header_row_index_a": 0,
        "entity_column_a": 0,
        "metric_column_a": 1,
        "token_b": b["token"],
        "header_row_index_b": 0,
        "entity_column_b": 0,
        "metric_column_b": 1,
    }

    resp = client.post("/api/tools/compare/export", json=body)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

    wb = openpyxl.load_workbook(BytesIO(resp.content))
    ws = wb["Расхождения"]
    header = [c.value for c in ws[1]]
    assert header == ["Сущность", "Сумма A", "Сумма B", "Разница", "Статус"]

    rows = list(ws.iter_rows(min_row=2, values_only=True))
    assert len(rows) == 4
    statuses = {r[4] for r in rows}
    assert statuses == {"совпадает", "расхождение", "только в A", "только в B"}


def test_compare_unknown_token_is_404(client):
    resp = client.post(
        "/api/tools/compare",
        json={
            "token_a": "missing",
            "header_row_index_a": 0,
            "entity_column_a": 0,
            "metric_column_a": 1,
            "token_b": "also-missing",
            "header_row_index_b": 0,
            "entity_column_b": 0,
            "metric_column_b": 1,
        },
    )
    assert resp.status_code == 404


# ---------------------------- Mail Merge ----------------------------


def _mailmerge_source_bytes() -> bytes:
    return _xlsx_bytes(
        [
            ["ФИО", "Сумма", "Дата"],
            ["Иванов Иван Иванович", 15000, "01.09.2026"],
            ["Петрова Мария Сергеевна", 8250, "02.09.2026"],
            ["Сидоров Пётр Ильич", 12000, "03.09.2026"],
        ]
    )


def _upload_for_mailmerge(client, content: bytes, name: str = "recipients.xlsx") -> dict:
    resp = client.post(
        "/api/tools/preview",
        files={"file": (name, content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_mailmerge_produces_one_pdf_per_row(client):
    import zipfile
    from io import BytesIO

    from pypdf import PdfReader

    preview = _upload_for_mailmerge(client, _mailmerge_source_bytes())

    resp = client.post(
        "/api/tools/mailmerge",
        json={
            "token": preview["token"],
            "header_row_index": 0,
            "template": "Уважаемый {{ФИО}},\nК оплате: {{Сумма}} руб. Дата: {{Дата}}.",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/zip")

    zf = zipfile.ZipFile(BytesIO(resp.content))
    names = zf.namelist()
    assert len(names) == 3  # 3 data rows

    # Every generated PDF must contain ITS OWN recipient's data, not some
    # other row's — the whole point of Mail Merge.
    combined_text = ""
    for name in names:
        pdf_bytes = zf.read(name)
        reader = PdfReader(BytesIO(pdf_bytes))
        combined_text += reader.pages[0].extract_text() or ""

    assert "Иванов Иван Иванович" in combined_text
    assert "15000" in combined_text
    assert "Петрова Мария Сергеевна" in combined_text
    assert "8250" in combined_text
    assert "Сидоров Пётр Ильич" in combined_text
    assert "12000" in combined_text


def test_mailmerge_rejects_unknown_placeholder(client):
    preview = _upload_for_mailmerge(client, _mailmerge_source_bytes())

    resp = client.post(
        "/api/tools/mailmerge",
        json={
            "token": preview["token"],
            "header_row_index": 0,
            "template": "Уважаемый {{Отчество}}",
        },
    )
    assert resp.status_code == 400


def test_mailmerge_unknown_token_is_404(client):
    resp = client.post(
        "/api/tools/mailmerge",
        json={"token": "does-not-exist", "header_row_index": 0, "template": "{{ФИО}}"},
    )
    assert resp.status_code == 404


# ---------------------------- Split by marker ----------------------------


def _make_invoices_pdf_bytes() -> bytes:
    from io import BytesIO
    from pathlib import Path

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    candidates = [
        Path(r"C:\Windows\Fonts\arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
    ]
    font = None
    for c in candidates:
        if c.exists():
            pdfmetrics.registerFont(TTFont("ApiTestCyrillicFont", str(c)))
            font = "ApiTestCyrillicFont"
            break
    assert font is not None, "No Cyrillic TTF font available for this test"

    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    for inv_num in range(1, 4):
        c.setFont(font, 12)
        c.drawString(70, 780, f"Накладная № {inv_num:04d}")
        c.drawString(70, 750, f"содержимое накладной {inv_num}")
        c.showPage()
    c.save()
    return buffer.getvalue()


def test_split_produces_one_file_per_marker(client):
    import zipfile
    from io import BytesIO

    from pypdf import PdfReader

    pdf_bytes = _make_invoices_pdf_bytes()
    resp = client.post(
        "/api/tools/split",
        files={"file": ("накладные.pdf", pdf_bytes, "application/pdf")},
        data={"marker": "Накладная №"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/zip")

    zf = zipfile.ZipFile(BytesIO(resp.content))
    names = zf.namelist()
    assert len(names) == 3

    found_numbers = set()
    for name in names:
        reader = PdfReader(BytesIO(zf.read(name)))
        assert len(reader.pages) == 1
        text = reader.pages[0].extract_text() or ""
        for n in ("0001", "0002", "0003"):
            if n in text:
                found_numbers.add(n)
    assert found_numbers == {"0001", "0002", "0003"}


def test_split_marker_not_found_is_400_not_empty_zip(client):
    pdf_bytes = _make_invoices_pdf_bytes()
    resp = client.post(
        "/api/tools/split",
        files={"file": ("накладные.pdf", pdf_bytes, "application/pdf")},
        data={"marker": "Такого текста тут точно нет"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]


def test_split_rejects_garbage_file(client):
    resp = client.post(
        "/api/tools/split",
        files={"file": ("not-a-pdf.pdf", b"garbage bytes, not a pdf", "application/pdf")},
        data={"marker": "Накладная"},
    )
    assert resp.status_code == 400


# ---------------------------- Watermark ----------------------------


def _make_simple_pdf_bytes(n_pages: int = 2) -> bytes:
    from io import BytesIO
    from pathlib import Path

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    candidates = [
        Path(r"C:\Windows\Fonts\arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
    ]
    font = None
    for c in candidates:
        if c.exists():
            pdfmetrics.registerFont(TTFont("ApiTestCyrillicFont2", str(c)))
            font = "ApiTestCyrillicFont2"
            break
    assert font is not None, "No Cyrillic TTF font available for this test"

    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    for i in range(n_pages):
        c.setFont(font, 12)
        c.drawString(70, 780, f"Прайс-лист, страница {i + 1}")
        c.showPage()
    c.save()
    return buffer.getvalue()


def test_watermark_appears_on_every_page(client):
    from io import BytesIO

    from pypdf import PdfReader

    pdf_bytes = _make_simple_pdf_bytes(3)
    resp = client.post(
        "/api/tools/watermark",
        files={"file": ("прайс.pdf", pdf_bytes, "application/pdf")},
        data={"text": "sotrudnik@example.com"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/pdf")

    reader = PdfReader(BytesIO(resp.content))
    assert len(reader.pages) == 3
    for page in reader.pages:
        text = page.extract_text() or ""
        assert "sotrudnik@example.com" in text


def test_watermark_rejects_empty_text(client):
    pdf_bytes = _make_simple_pdf_bytes(1)
    resp = client.post(
        "/api/tools/watermark",
        files={"file": ("прайс.pdf", pdf_bytes, "application/pdf")},
        data={"text": "   "},
    )
    assert resp.status_code == 400


def test_watermark_rejects_garbage_file(client):
    resp = client.post(
        "/api/tools/watermark",
        files={"file": ("not-a-pdf.pdf", b"garbage bytes, not a pdf", "application/pdf")},
        data={"text": "ЧЕРНОВИК"},
    )
    assert resp.status_code == 400
