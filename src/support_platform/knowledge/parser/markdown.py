"""Markdown / plain-text parser with line locators."""

from __future__ import annotations

from pathlib import Path

from support_platform.knowledge.parser.types import ParseError, ParsedChunk


def parse_markdown(path: Path) -> list[ParsedChunk]:
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ParseError("empty content: markdown file has no extractable text")

    lines = text.splitlines()
    # One chunk per non-empty paragraph block, tracking line numbers (1-based).
    chunks: list[ParsedChunk] = []
    buf: list[str] = []
    start: int | None = None

    def flush(end_line: int) -> None:
        nonlocal buf, start
        if not buf or start is None:
            buf = []
            start = None
            return
        content = "\n".join(buf).strip()
        if content:
            chunks.append(
                ParsedChunk(
                    content=content,
                    locator={"kind": "md", "line_start": start, "line_end": end_line},
                )
            )
        buf = []
        start = None

    for idx, line in enumerate(lines, start=1):
        if line.strip() == "":
            if buf:
                flush(idx - 1)
            continue
        if start is None:
            start = idx
        buf.append(line)
    if buf and start is not None:
        flush(len(lines))

    if not chunks:
        raise ParseError("empty content: no markdown chunks produced")
    return chunks


def parse_txt(path: Path) -> list[ParsedChunk]:
    chunks = parse_markdown(path)
    for chunk in chunks:
        chunk.locator = {
            "kind": "txt",
            "line_start": chunk.locator["line_start"],
            "line_end": chunk.locator["line_end"],
        }
    return chunks
