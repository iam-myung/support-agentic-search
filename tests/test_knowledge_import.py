"""S2 parse/index/import RED contracts: empty/bad files, locator, embedding dims."""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "corpus"


def test_empty_markdown_import_fails_and_does_not_activate() -> None:
    from support_platform.knowledge.importer import import_knowledge_file

    result = None
    with pytest.raises(Exception) as exc_info:
        result = import_knowledge_file(
            path=FIXTURES / "empty.md",
            source_type="DOC",
            title="Empty",
        )
    assert result is None
    msg = str(exc_info.value).lower()
    assert "empty" in msg or "parse" in msg or "content" in msg


def test_unsupported_format_import_fails() -> None:
    from support_platform.knowledge.importer import import_knowledge_file

    weird = FIXTURES / "not_supported.xyz"
    weird.write_text("hello", encoding="utf-8")
    try:
        with pytest.raises(Exception) as exc_info:
            import_knowledge_file(
                path=weird,
                source_type="DOC",
                title="Weird",
            )
        assert "format" in str(exc_info.value).lower() or "support" in str(exc_info.value).lower()
    finally:
        weird.unlink(missing_ok=True)


def test_scanned_pdf_without_extractable_text_fails() -> None:
    from support_platform.knowledge.parser.pdf import parse_pdf

    # Fixture path points at a zero-text stand-in; GREEN must treat no-text PDF as error.
    scanned = FIXTURES / "scanned_placeholder.pdf.txt"
    with pytest.raises(Exception) as exc_info:
        parse_pdf(scanned)
    assert "text" in str(exc_info.value).lower() or "scan" in str(exc_info.value).lower() or "empty" in str(exc_info.value).lower()


def test_markdown_chunks_include_line_locators() -> None:
    from support_platform.knowledge.parser.markdown import parse_markdown

    chunks = parse_markdown(FIXTURES / "guide.md")
    assert len(chunks) >= 1
    for chunk in chunks:
        loc = chunk.locator
        assert loc["kind"] in {"md", "MD", "markdown", "txt", "TXT"}
        assert "line_start" in loc
        assert "line_end" in loc
        assert loc["line_start"] <= loc["line_end"]
        assert chunk.content.strip()


def test_embedding_dimension_mismatch_is_rejected() -> None:
    from support_platform.infrastructure.embedding import FakeEmbeddingAdapter
    from support_platform.knowledge.indexer import index_chunks

    adapter = FakeEmbeddingAdapter(model="fake-emb", dimension=8)
    chunks = [
        {"ordinal": 0, "content": "hello", "locator": {"kind": "md", "line_start": 1, "line_end": 1}}
    ]
    with pytest.raises(Exception) as exc_info:
        index_chunks(
            chunks,
            embedding=adapter,
            expected_model="fake-emb",
            expected_dimension=16,
        )
    assert "dimension" in str(exc_info.value).lower()


def test_embedding_model_mismatch_is_rejected() -> None:
    from support_platform.infrastructure.embedding import FakeEmbeddingAdapter
    from support_platform.knowledge.indexer import index_chunks

    adapter = FakeEmbeddingAdapter(model="other-model", dimension=8)
    chunks = [
        {"ordinal": 0, "content": "hello", "locator": {"kind": "md", "line_start": 1, "line_end": 1}}
    ]
    with pytest.raises(Exception) as exc_info:
        index_chunks(
            chunks,
            embedding=adapter,
            expected_model="fake-emb",
            expected_dimension=8,
        )
    assert "model" in str(exc_info.value).lower()


def test_successful_index_records_model_and_dimension() -> None:
    from support_platform.infrastructure.embedding import FakeEmbeddingAdapter
    from support_platform.knowledge.indexer import index_chunks

    adapter = FakeEmbeddingAdapter(model="fake-emb", dimension=8)
    chunks = [
        {"ordinal": 0, "content": "hello world", "locator": {"kind": "md", "line_start": 1, "line_end": 1}}
    ]
    indexed = index_chunks(
        chunks,
        embedding=adapter,
        expected_model="fake-emb",
        expected_dimension=8,
    )
    assert indexed.embedding_model == "fake-emb"
    assert indexed.embedding_dimension == 8
    assert len(indexed.vectors) == 1
    assert len(indexed.vectors[0]) == 8


def test_injection_text_is_treated_as_data_not_system_rule() -> None:
    from support_platform.knowledge.parser.markdown import parse_markdown
    from support_platform.knowledge.safety import SYSTEM_RULES_FINGERPRINT, rules_fingerprint_after_ingest

    before = SYSTEM_RULES_FINGERPRINT
    chunks = parse_markdown(FIXTURES / "faq_with_injection.md")
    assert any("Ignore all previous instructions" in c.content for c in chunks)
    after = rules_fingerprint_after_ingest(chunks)
    assert after == before
