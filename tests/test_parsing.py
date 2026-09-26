"""Tests parsing against real files on disk — including a genuine
Windows-1251, semicolon-delimited CSV (the actual 1C export shape), not
just a UTF-8 happy path. This is the exact failure mode ("кракозябры")
identified as the real risk for our target audience."""

import polars as pl
import pytest

from core.parsing import UnsupportedFileError, normalize_decimal_comma, parse_file


@pytest.fixture
def utf8_csv(tmp_path):
    path = tmp_path / "utf8.csv"
    path.write_text("Товар,Сумма\nНоски,100.50\nШапка,250.00\n", encoding="utf-8-sig")
    return path


@pytest.fixture
def cp1251_semicolon_csv(tmp_path):
    """Mirrors a real 1C export: Windows-1251 encoding, semicolon
    delimiter, decimal comma in numbers — every trap in one file."""
    path = tmp_path / "1c_export.csv"
    content = "Товар;Сумма\nНоски;1 234,56\nШапка;99,90\n"
    path.write_bytes(content.encode("cp1251"))
    return path


def test_utf8_csv_parses_cleanly(utf8_csv):
    result = parse_file(utf8_csv)
    assert result.detected_delimiter == ","
    assert result.df.shape == (2, 2)
    assert result.df["Товар"].to_list() == ["Носки", "Шапка"]


def test_cp1251_semicolon_csv_detected_and_parsed_without_mojibake(cp1251_semicolon_csv):
    """The core claim this module exists to prove: a real 1C-shaped CSV
    must not turn into 'кракозябры'."""
    result = parse_file(cp1251_semicolon_csv)
    assert result.detected_delimiter == ";"
    assert result.detected_encoding is not None
    assert "1251" in result.detected_encoding.lower().replace("-", "")
    # The actual proof: Cyrillic text must come through byte-for-byte correct.
    assert result.df["Товар"].to_list() == ["Носки", "Шапка"]


def test_decimal_comma_normalized_to_float(cp1251_semicolon_csv):
    result = parse_file(cp1251_semicolon_csv)
    normalized = normalize_decimal_comma(result.df, "Сумма")
    assert normalized.to_list() == [1234.56, 99.90]


def test_unsupported_extension_raises():
    with pytest.raises(UnsupportedFileError):
        parse_file("report.pdf")


def test_xlsx_round_trip(tmp_path):
    path = tmp_path / "report.xlsx"
    original = pl.DataFrame({"Товар": ["Носки", "Шапка"], "Сумма": [100.5, 250.0]})
    original.write_excel(path)

    result = parse_file(path)
    assert result.detected_encoding is None  # not applicable to Excel
    assert result.df["Товар"].to_list() == ["Носки", "Шапка"]
    assert result.df["Сумма"].to_list() == [100.5, 250.0]
