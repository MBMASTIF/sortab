"""Splits one multi-page PDF into several PDFs at every page a given text
marker appears on ("Разбивка по маркеру" — README "Каталог услуг" item 8 /
Фаза 4): the real target is a stack of N накладных concatenated into one
PDF export, where each накладная's first page repeats a marker like
"Накладная №" or the customer's ИНН — this puts them back apart.

Library choice (pypdf, benchmarked against pikepdf for this phase — see
core/watermark.py's docstring for the full comparison and numbers): this
task fundamentally needs per-page TEXT extraction to find the marker.
pikepdf is a structural/QDF editing library built on libqpdf — it has no
text-layout engine at all (verified live: `hasattr` check for any text
extraction API on its Page/Pdf objects comes back False). pypdf has one
built in (`page.extract_text()`) and was fast enough on both benchmarked
tasks, so one library covers Split and Watermark instead of two.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError


class SplitError(ValueError):
    pass


@dataclass
class SplitPart:
    name: str
    pdf_bytes: bytes
    page_count: int


def split_by_marker(pdf_bytes: bytes, marker: str) -> list[SplitPart]:
    """Every page whose extracted text contains `marker` (a plain substring
    match — case-sensitive and exact, so the user's own wording, e.g.
    "Накладная №0042" vs. just "Накладная №", decides how coarse or
    precise the split is) starts a new output document that runs up to
    (not including) the next marker page, or the end of the PDF.

    If the file's very first page(s) come before any marker occurrence,
    they're kept as a leading part rather than silently dropped — the
    caller gets every page back across the returned parts, always.

    Raises SplitError if the marker matches on zero pages: an empty result
    would otherwise turn into a technically-successful-but-useless empty
    .zip on the API side (see README instructions: "если маркер не найден
    — понятная ошибка, не пустой архив молча").
    """
    marker = marker.strip()
    if not marker:
        raise SplitError("Маркер не может быть пустым")

    try:
        reader = PdfReader(BytesIO(pdf_bytes))
    except (PdfReadError, ValueError) as exc:
        raise SplitError(f"Не удалось прочитать PDF: {exc}") from exc

    if reader.is_encrypted:
        raise SplitError("PDF защищён паролем — разбивка невозможна")

    n_pages = len(reader.pages)
    if n_pages == 0:
        raise SplitError("PDF не содержит страниц")

    marker_pages = [i for i in range(n_pages) if marker in (reader.pages[i].extract_text() or "")]

    if not marker_pages:
        raise SplitError(f"Маркер {marker!r} не найден ни на одной странице PDF")

    boundaries = marker_pages if marker_pages[0] == 0 else [0, *marker_pages]

    parts: list[SplitPart] = []
    for idx, start in enumerate(boundaries):
        end = boundaries[idx + 1] if idx + 1 < len(boundaries) else n_pages
        writer = PdfWriter()
        for page_index in range(start, end):
            writer.add_page(reader.pages[page_index])
        buffer = BytesIO()
        writer.write(buffer)
        # 1-based, human-facing numbering — "часть_1", "часть_2", ... — the
        # caller (backend/main.py) is free to rename using extracted data,
        # this is just a safe, always-available default.
        parts.append(SplitPart(name=f"часть_{idx + 1}", pdf_bytes=buffer.getvalue(), page_count=end - start))

    return parts
