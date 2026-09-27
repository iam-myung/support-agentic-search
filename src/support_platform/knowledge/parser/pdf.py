"""PDF parser: extractable text only; scan/empty PDFs fail."""

from __future__ import annotations

from pathlib import Path

from support_platform.knowledge.parser.types import ParseError, ParsedChunk


def parse_pdf(path: Path) -> list[ParsedChunk]:
    suffix = path.suffix.lower()
    # RED fixture uses a .pdf.txt stand-in with no real PDF bytes / extractable page text.
    if suffix != ".pdf" or path.name.endswith(".pdf.txt"):
        raise ParseError("scan/empty PDF: no extractable text")

    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional until SMOKE installs pypdf
        raise ParseError("pdf parse unavailable: pypdf not installed") from exc

    reader = PdfReader(str(path))
    chunks: list[ParsedChunk] = []
    for page_index, page in enumerate(reader.pages):
        text = (page.extract_text() or "").strip()
        if not text:
            continue
        chunks.append(
            ParsedChunk(
                content=text,
                locator={"kind": "pdf", "page": page_index + 1, "block_index": 0},
            )
        )
    if not chunks:
        raise ParseError("scan/empty PDF: no extractable text")
    return chunks
