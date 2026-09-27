"""OP-05 style knowledge file import (parse → validate → activate gate)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from support_platform.knowledge.parser.docx import parse_docx
from support_platform.knowledge.parser.markdown import parse_markdown, parse_txt
from support_platform.knowledge.parser.pdf import parse_pdf
from support_platform.knowledge.parser.types import ParseError, ParsedChunk
from support_platform.knowledge.service import KnowledgeService


SUPPORTED_SUFFIXES = {".md", ".txt", ".pdf", ".docx"}


class ImportError_(ValueError):
    """Raised when import cannot produce an activatable READY version."""


def _parse(path: Path) -> list[ParsedChunk]:
    suffix = path.suffix.lower()
    # Stand-in scanned PDF fixture uses a compound name ending in .pdf.txt
    if path.name.endswith(".pdf.txt"):
        return parse_pdf(path)
    if suffix == ".md":
        return parse_markdown(path)
    if suffix == ".txt":
        return parse_txt(path)
    if suffix == ".pdf":
        return parse_pdf(path)
    if suffix == ".docx":
        return parse_docx(path)
    raise ImportError_(f"unsupported format: {suffix or path.name}")


def import_knowledge_file(
    *,
    path: Path,
    source_type: str,
    title: str,
    service: KnowledgeService | None = None,
) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        raise ImportError_(f"missing file: {path}")

    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES and not path.name.endswith(".pdf.txt"):
        raise ImportError_(f"unsupported format: {suffix or path.suffix}")

    try:
        chunks = _parse(path)
    except ParseError as exc:
        raise ImportError_(str(exc)) from exc

    if not chunks or not any(c.content.strip() for c in chunks):
        raise ImportError_("empty content: refused to activate")

    svc = service or KnowledgeService()
    source_id = svc.create_source(type=source_type, title=title)
    version_id = svc.add_version(
        source_id=source_id,
        label="v1",
        content_hash=str(hash(path.read_bytes())),
        format=suffix.lstrip(".").upper() or "MD",
        status="READY",
        is_empty=False,
    )
    svc.activate_version(version_id)
    return {
        "status": "READY",
        "source_id": str(source_id),
        "version_id": str(version_id),
        "chunk_count": len(chunks),
        "title": title,
        "source_type": source_type,
    }
