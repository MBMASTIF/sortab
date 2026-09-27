"""File ingestion: turns an uploaded Excel/CSV/ODS file into a Polars
DataFrame plus a column preview, using the stack validated by benchmark
(see README): python-calamine for the Excel family, Polars' native CSV
reader with charset-normalizer-driven encoding detection for CSV.
"""

from dataclasses import dataclass
from pathlib import Path

import polars as pl
from charset_normalizer import from_bytes

EXCEL_SUFFIXES = {".xls", ".xlsx", ".xlsm", ".xlsb", ".ods"}
CSV_SUFFIXES = {".csv", ".txt"}


class UnsupportedFileError(ValueError):
    pass


@dataclass
class ParsedFile:
    df: pl.DataFrame
    detected_encoding: str | None  # None for Excel-family (encoding is binary, not text)
    detected_delimiter: str | None


def _detect_csv_dialect(raw: bytes) -> tuple[str, str]:
    """Returns (encoding, delimiter). Order matters and is deliberate:

    1. UTF-8 first (strict — fails loudly on non-UTF-8 bytes, so this step
       can never silently misfire).
    2. Windows-1251 SECOND, tried explicitly before any generic detector.
       Verified live (2026-09-27): charset-normalizer mis-detected a real
       cp1251 sample as cp1255 (Hebrew) — both are single-byte codepages
       and a short/ambiguous sample isn't enough signal to tell them apart
       statistically. Since our entire target audience is Russian business
       software (1C exports default to cp1251), domain knowledge beats a
       generic guesser here — we're not guessing, we're asserting what we
       already know is true for this market.
    3. charset-normalizer only as a last-resort fallback, for the rare file
       that's neither UTF-8 nor cp1251 (some other legacy encoding).
    """
    try:
        raw.decode("utf-8-sig")
        return "utf-8-sig", _guess_delimiter(raw.decode("utf-8-sig")[:5000])
    except UnicodeDecodeError:
        pass

    try:
        sample = raw.decode("cp1251")
        return "cp1251", _guess_delimiter(sample[:5000])
    except UnicodeDecodeError:
        pass

    best = from_bytes(raw).best()
    if best is None:
        raise UnsupportedFileError(
            "Could not detect a text encoding for this CSV — file may be corrupt"
        )
    encoding = best.encoding
    sample = str(best)
    return encoding, _guess_delimiter(sample[:5000])


def _guess_delimiter(sample: str) -> str:
    semicolon_count = sample.count(";")
    comma_count = sample.count(",")
    # Semicolon wins ties: it's the 1C/Russian-Excel default, and a comma
    # inside a semicolon-delimited file is far more likely (decimal commas,
    # thousands separators) than the reverse.
    return ";" if semicolon_count >= comma_count else ","


def parse_file(path: str | Path) -> ParsedFile:
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix in EXCEL_SUFFIXES:
        df = pl.read_excel(path)
        return ParsedFile(df=df, detected_encoding=None, detected_delimiter=None)

    if suffix in CSV_SUFFIXES:
        raw = path.read_bytes()
        encoding, delimiter = _detect_csv_dialect(raw)
        df = pl.read_csv(
            path,
            encoding=encoding,
            separator=delimiter,
            # Russian financial exports use decimal commas ("1234,56") —
            # Polars' CSV reader doesn't have a decimal-comma flag the way
            # pandas does, so numeric columns arrive as strings here and
            # get normalized explicitly, not silently mis-parsed as text.
            infer_schema_length=0,
        )
        return ParsedFile(df=df, detected_encoding=encoding, detected_delimiter=delimiter)

    raise UnsupportedFileError(f"Unsupported file extension: {suffix!r}")


def parse_file_raw(path: str | Path) -> ParsedFile:
    """Reads a file with NO assumption about where the header row is —
    every row, including whatever would normally become the column header,
    comes back as plain data with generic column names ("column_1",
    "column_2", ...).

    Why this exists as a separate function rather than a flag inside
    parse_file(): a real 1C/WB export frequently has a decorative title
    row ("Отчёт за сентябрь 2026") above the real header, and the
    upload screen needs to show the user the raw grid so they can click
    the row that's actually the header. parse_file() must keep assuming
    row 0 is the header (existing tests + the "no decision needed" fast
    path both depend on that), so this is an additive sibling, not a
    behavior change to the tested function.

    Once the user confirms the real header row index (via the API's
    /columns step), the caller slices this raw DataFrame at that index
    and promotes that row's cell values to real column names — see
    backend/main.py.
    """
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix in EXCEL_SUFFIXES:
        df = pl.read_excel(path, has_header=False, drop_empty_rows=False, drop_empty_cols=False)
        return ParsedFile(df=df, detected_encoding=None, detected_delimiter=None)

    if suffix in CSV_SUFFIXES:
        raw = path.read_bytes()
        encoding, delimiter = _detect_csv_dialect(raw)
        df = pl.read_csv(
            path,
            encoding=encoding,
            separator=delimiter,
            has_header=False,
            infer_schema_length=0,
        )
        return ParsedFile(df=df, detected_encoding=encoding, detected_delimiter=delimiter)

    raise UnsupportedFileError(f"Unsupported file extension: {suffix!r}")


def normalize_decimal_comma(df: pl.DataFrame, column: str) -> pl.Series:
    """Converts a Russian-formatted numeric text column ("1 234,56") into
    a proper float column. Kept as a separate explicit step (not silently
    auto-applied in parse_file) because a column only gets this treatment
    once the user has confirmed it's actually a money/number column on the
    mapping screen — guessing wrong here would corrupt real numbers."""
    return (
        df[column]
        .str.strip_chars()
        .str.replace_all(r"\s", "")  # thousands separator: "1 234,56"
        .str.replace(",", ".")
        .cast(pl.Float64, strict=False)
    )
