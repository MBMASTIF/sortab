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
PDF_SUFFIXES = {".pdf"}


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

    if suffix in PDF_SUFFIXES:
        df = _read_pdf_raw(path)
        return ParsedFile(df=df, detected_encoding=None, detected_delimiter=None)

    raise UnsupportedFileError(f"Unsupported file extension: {suffix!r}")


def _read_pdf_raw(path: Path) -> pl.DataFrame:
    """Extracts every table camelot finds in a live (non-scanned) PDF and
    returns it in the exact same shape parse_file_raw() gives Excel/CSV: a
    2D grid with no header assumption and generic "column_N" names, so the
    rest of the pipeline (backend/columns.py::finalize_columns and the
    upload.html header-row-click UI) needs zero changes to work with PDF.

    Library/flavor choice (camelot, flavor="stream") is the benchmarked,
    already-decided stack — see README "PDF — реально протестировано":
    pdfplumber's default line-border strategy finds 0 rows on real invoices
    that have no drawn cell borders; camelot's stream flavor (text-position
    based, not border based) is the one that actually works on that shape.
    Import is local to this function, not top-of-module: camelot pulls in
    a fairly heavy dependency chain (matplotlib, opencv, pdfminer) that
    every Excel/CSV-only request would otherwise pay to import for nothing.
    """
    import gc

    import camelot
    from playa.exceptions import PDFException

    try:
        tables = camelot.read_pdf(str(path), flavor="stream", pages="all")
    except (PDFException, StopIteration, ValueError) as exc:
        # camelot's backend (playa) does NOT raise one clean, documented
        # exception type for "this isn't a readable PDF" — verified live
        # by throwing a battery of malformed .pdf-extension inputs at it,
        # not assumed from the docs:
        #   - most malformed content -> playa.exceptions.PDFException
        #     (its own hierarchy: syntax, encryption, font errors, ...)
        #   - some truncated/malformed content -> a bare StopIteration
        #     escapes from playa's trailer reader (document.py's
        #     `next(...)` has no except of its own) — caught HERE, in a
        #     plain sync frame, deliberately, so it never gets anywhere
        #     near an `async def` boundary: PEP 479 turns an escaping
        #     StopIteration into "RuntimeError: coroutine raised
        #     StopIteration" once it crosses one, which is exactly what
        #     an earlier version of this function let happen (confirmed
        #     against the live server's error log: an unhandled 500).
        #   - a genuinely empty file -> ValueError("cannot mmap an empty
        #     file") straight out of the stdlib mmap module, before playa
        #     even starts parsing.
        # All three mean the same thing to our caller: this .pdf can't be
        # read, treat it like any other unsupported input, not a crash.
        #
        # exc.__traceback__ is cleared, and chaining suppressed (`from
        # None`), before re-raising: playa mmaps the source file, and the
        # caught exception's traceback keeps the frame that mmap'd it
        # alive (an in-flight exception's traceback is a live reference,
        # not GC-collectible garbage — gc.collect() alone can't free it).
        # Verified live on Windows: without clearing the traceback here,
        # the caller's own `finally: os.unlink(tmp_path)` (see
        # backend/main.py's /api/upload) fails with PermissionError
        # because the OS won't delete a file that's still memory-mapped.
        exc.__traceback__ = None
        raise UnsupportedFileError(f"Could not read PDF ({exc}): {path}") from None

    if len(tables) == 0:
        raise UnsupportedFileError(
            f"No tables detected in PDF (camelot, flavor=stream): {path}"
        )

    # Camelot hands back one Table per page a table was found on. NaN
    # marks a cell camelot found no text for at all — normalize to "" so
    # every value downstream is a plain string, same as Excel/CSV raw rows.
    page_grids: list[list[list[str]]] = [
        table.df.fillna("").astype(str).values.tolist() for table in tables
    ]

    # camelot's PDF backend (playa) mmaps the source file for speed. That
    # mmap stays alive as long as anything reachable still references the
    # TableList — verified live: dropping `tables` here and forcing a GC
    # pass releases the mmap immediately, whereas relying on refcounting
    # alone does not (there's a reference cycle in camelot/playa's object
    # graph that CPython's refcounter can't resolve on its own). Without
    # this, the caller (backend/main.py's /api/upload, which writes the
    # upload to a NamedTemporaryFile and os.unlink()s it in a `finally`)
    # gets a PermissionError on Windows because the OS won't delete a file
    # that's still memory-mapped open elsewhere in the process. All the
    # data we need is already copied into page_grids above as plain
    # strings, so nothing is lost by releasing camelot's objects now.
    del tables
    gc.collect()

    grid = _glue_pdf_page_grids(page_grids)

    ncols = max((len(row) for row in grid), default=0)
    columns = [f"column_{i + 1}" for i in range(ncols)]
    padded_rows = [row + [""] * (ncols - len(row)) for row in grid]

    if ncols == 0:
        return pl.DataFrame(schema=columns)
    return pl.DataFrame(padded_rows, schema=columns, orient="row")


def _glue_pdf_page_grids(page_grids: list[list[list[str]]]) -> list[list[str]]:
    """Decides how to combine camelot's one-table-per-page result into the
    single grid parse_file_raw() must return.

    Architecture decision (deliberate, not guessed — see task brief): a
    multi-page PDF report is overwhelmingly one logical table split across
    pages, with the header row repeated verbatim on every page (the common
    real-world pattern for exported reports). So:

    - If every page's first row is byte-for-byte identical (and all pages
      share the same column count, so "first row" unambiguously means the
      same thing everywhere), treat it as that pattern: keep page 1 in
      full, drop the repeated header row from every subsequent page. This
      is the one case we're confident enough to act on automatically.
    - Otherwise (headers differ across pages, or camelot found a different
      number of columns on different pages — a sign its per-page grid
      detection itself diverged) we do NOT try to guess which rows are
      "real" headers vs data. We concatenate every page's rows as-is, in
      page order, padding short rows to the widest row seen so the grid
      stays rectangular (a hard requirement to build one Polars
      DataFrame). The existing upload.html UI is already built for this
      exact situation — it shows the raw grid and lets a human click the
      real header row — so handing it an unedited, page-ordered grid is a
      correct, honest fallback, not a workaround.
    """
    if len(page_grids) <= 1:
        return page_grids[0] if page_grids else []

    non_empty_grids = [g for g in page_grids if g]
    widths = {len(row) for grid in non_empty_grids for row in grid}
    same_header_everywhere = (
        len(widths) == 1
        and all(grid[0] == non_empty_grids[0][0] for grid in non_empty_grids[1:])
    )

    if same_header_everywhere:
        glued = list(non_empty_grids[0])
        for grid in non_empty_grids[1:]:
            glued.extend(grid[1:])
        return glued

    # Fallback: honest concatenation, no header dedup, padded to rectangular.
    all_rows = [row for grid in page_grids for row in grid]
    max_width = max((len(row) for row in all_rows), default=0)
    return [row + [""] * (max_width - len(row)) for row in all_rows]


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
