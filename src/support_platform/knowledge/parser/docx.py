"""DOCX parser with paragraph locators."""

from __future__ import annotations

from pathlib import Path

from support_platform.knowledge.parser.types import ParseError, ParsedChunk


def parse_docx(path: Path) -> list[ParsedChunk]:
    try:
        from docx import Document  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise ParseError("docx parse unavailable: python-docx not installed") from exc

    document = Document(str(path))
    chunks: list[ParsedChunk] = []
    heading_path: list[str] = []
    for index, paragraph in enumerate(document.paragraphs):
        text = (paragraph.text or "").strip()
        style_name = getattr(getattr(paragraph, "style", None), "name", "") or ""
        if style_name.startswith("Heading") and text:
            level = int(style_name.replace("Heading", "").strip() or "1")
            heading_path = heading_path[: max(level - 1, 0)] + [text]
            continue
        if not text:
            continue
        chunks.append(
            ParsedChunk(
                content=text,
                locator={
                    "kind": "docx",
                    "paragraph_index": index,
                    "heading_path": list(heading_path),
                },
            )
        )
    if not chunks:
        raise ParseError("empty content: docx has no extractable paragraphs")
    return chunks
