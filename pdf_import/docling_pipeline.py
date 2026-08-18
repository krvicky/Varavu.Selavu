"""docling-based PDF table/text extraction.

Table structure recognition ON, mode ACCURATE (bank tables are dense;
fast mode drops/merges cells). OCR is off by default -- parse_pdf() checks
each page for a native text layer via pypdf (not docling internals, which
don't expose this directly) and re-runs the whole document with OCR on if
any page is scanned/image-only.
"""
import os
from pathlib import Path

# docling's layout model wraps inference in torch.compile(); the default
# "inductor" backend needs a C++ toolchain (MSVC cl.exe on Windows, gcc on
# Linux) and fails hard when one isn't installed. Eager mode is functionally
# identical, just slower, so disable compilation unless the user opted in.
# Must be set before torch is first imported (docling imports it below).
os.environ.setdefault("TORCH_COMPILE_DISABLE", "1")

import pypdf
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
from docling.document_converter import DocumentConverter, PdfFormatOption

TableFrame = list[list[str]]


def _build_converter(ocr: bool) -> DocumentConverter:
    pipeline_options = PdfPipelineOptions(do_table_structure=True)
    pipeline_options.table_structure_options.mode = TableFormerMode.ACCURATE
    pipeline_options.do_ocr = ocr
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
    )


def convert(path: Path, ocr: bool = False):
    result = _build_converter(ocr).convert(str(path))
    return result.document


def extract_tables(doc) -> list[TableFrame]:
    """One TableFrame per table docling detected, in document order.

    Built from the table's raw cell grid (not export_to_dataframe()) so the
    header row stays an ordinary row instead of being consumed into a
    DataFrame's column index -- merge_pages() needs it to match against the
    adapter's header_signature and strip repeats itself.
    """
    frames = []
    for table in doc.tables:
        data = table.data
        grid = [["" for _ in range(data.num_cols)] for _ in range(data.num_rows)]
        for cell in data.table_cells:
            text = (cell.text or "").strip()
            for r in range(cell.start_row_offset_idx, cell.end_row_offset_idx):
                for c in range(cell.start_col_offset_idx, cell.end_col_offset_idx):
                    if r < data.num_rows and c < data.num_cols:
                        grid[r][c] = text
        frames.append(grid)
    return frames


def merge_pages(tables: list[TableFrame], header_signature: list[str]) -> TableFrame:
    """Concatenate the tables that look like the target transaction table
    into one logical table.

    docling detects every table on the page, not just the transaction
    table -- a multi-section document (e.g. a credit-card statement with a
    dues-summary table, a rewards-points table, a GST table alongside the
    domestic/international transaction tables) needs those non-transaction
    tables filtered out. A table is kept when its first couple of rows
    collectively mention every header_signature keyword (a fuzzy
    containment check, not positional equality -- column counts can differ
    between the first table of a section and its continuation pages).
    Row-level junk (repeated headers, section titles, footer text) is left
    to the adapter's skip_row_patterns, which match on content regardless
    of column layout.
    """
    keywords = [str(h).strip().lower() for h in header_signature if str(h).strip()]
    merged: TableFrame = []
    for frame in tables:
        preamble = " ".join(str(cell).strip().lower() for row in frame[:2] for cell in row)
        if not all(keyword in preamble for keyword in keywords):
            continue
        merged.extend(frame)
    return merged


def extract_statement_text(doc) -> str:
    return doc.export_to_markdown(strict_text=True)


def page_has_text_layer(path: Path, page_no: int) -> bool:
    reader = pypdf.PdfReader(str(path))
    text = (reader.pages[page_no - 1].extract_text() or "").strip()
    return len(text) > 0


def _page_count(path: Path) -> int:
    return len(pypdf.PdfReader(str(path)).pages)


def parse_pdf(path: Path, header_signature: list[str]) -> tuple[TableFrame, str, bool]:
    """Parse a (decrypted) PDF into a merged table + statement text.

    Returns (merged_table, statement_text, ocr_used).
    """
    doc = convert(path, ocr=False)
    ocr_used = any(
        not page_has_text_layer(path, page_no)
        for page_no in range(1, _page_count(path) + 1)
    )
    if ocr_used:
        doc = convert(path, ocr=True)

    tables = extract_tables(doc)
    merged = merge_pages(tables, header_signature)
    text = extract_statement_text(doc)
    return merged, text, ocr_used
