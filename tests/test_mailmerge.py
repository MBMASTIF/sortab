from io import BytesIO

import pytest
from pypdf import PdfReader

from core.mailmerge import (
    MailMergeError,
    build_row_dicts,
    extract_placeholders,
    render_pdf,
    render_text,
    validate_template,
)


def test_extract_placeholders_dedupes_and_preserves_order():
    template = "Уважаемый {{ФИО}}, ваша сумма {{Сумма}} руб. Ещё раз, {{ФИО}}."
    assert extract_placeholders(template) == ["ФИО", "Сумма"]


def test_extract_placeholders_tolerates_internal_whitespace():
    assert extract_placeholders("{{ ФИО }}") == ["ФИО"]


def test_validate_template_rejects_no_placeholders():
    with pytest.raises(MailMergeError):
        validate_template("Просто текст без плейсхолдеров", ["ФИО", "Сумма"])


def test_validate_template_rejects_unknown_column():
    with pytest.raises(MailMergeError):
        validate_template("Привет, {{Отчество}}", ["ФИО", "Сумма"])


def test_validate_template_accepts_known_columns():
    placeholders = validate_template("{{ФИО}}: {{Сумма}}", ["ФИО", "Сумма", "Дата"])
    assert placeholders == ["ФИО", "Сумма"]


def test_render_text_substitutes_and_blanks_missing():
    row = {"ФИО": "Иванов Иван", "Сумма": "1500"}
    result = render_text("Уважаемый {{ФИО}}, к оплате {{Сумма}} руб. Скидка: {{Скидка}}", row)
    assert result == "Уважаемый Иванов Иван, к оплате 1500 руб. Скидка: "


def test_build_row_dicts_stringifies_and_handles_none():
    column_names = ["ФИО", "Сумма"]
    rows = [["Иванов Иван", 1500], ["Петров Пётр", None]]
    result = build_row_dicts(column_names, rows)
    assert result == [
        {"ФИО": "Иванов Иван", "Сумма": "1500"},
        {"ФИО": "Петров Пётр", "Сумма": ""},
    ]


def test_render_pdf_produces_readable_cyrillic_text():
    text = "Уважаемый Иванов Иван Иванович,\nК оплате: 15000 руб."
    pdf_bytes = render_pdf(text)
    reader = PdfReader(BytesIO(pdf_bytes))
    assert len(reader.pages) == 1
    extracted = reader.pages[0].extract_text() or ""
    assert "Иванов Иван Иванович" in extracted
    assert "15000" in extracted


def test_render_pdf_wraps_and_paginates_long_text():
    # A single very long paragraph should wrap onto multiple lines, and
    # enough repeated paragraphs should overflow onto a second page rather
    # than being clipped or raising.
    long_paragraph = "слово " * 2000
    pdf_bytes = render_pdf(long_paragraph)
    reader = PdfReader(BytesIO(pdf_bytes))
    assert len(reader.pages) >= 2
    combined = "".join((p.extract_text() or "") for p in reader.pages)
    assert combined.count("слово") >= 1800  # wrapping didn't drop most of the words
