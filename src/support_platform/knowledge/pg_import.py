"""Persist parsed+indexed knowledge into real PostgreSQL (S2 SMOKE path)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine, text

from support_platform.infrastructure.embedding import HttpEmbeddingAdapter
from support_platform.knowledge.importer import ImportError_, _parse
from support_platform.knowledge.indexer import index_chunks


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def require_real_embedding_env() -> dict[str, str]:
    _load_dotenv()
    required = [
        "DATABASE_URL",
        "EMBEDDING_BASE_URL",
        "EMBEDDING_API_KEY",
        "EMBEDDING_MODEL",
        "EMBEDDING_DIMENSION",
    ]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"NOT_EXECUTED missing env: {', '.join(missing)}")
    return {k: os.environ[k] for k in required}


def persist_import_file(
    *,
    path: Path,
    source_type: str,
    title: str,
    database_url: str | None = None,
) -> dict[str, Any]:
    """Parse → real Embedding index → activate in PostgreSQL."""
    env = require_real_embedding_env()
    url = database_url or env["DATABASE_URL"]
    path = Path(path)
    if not path.exists():
        raise ImportError_(f"missing file: {path}")

    chunks = _parse(path)
    if not chunks or not any(c.content.strip() for c in chunks):
        raise ImportError_("empty content: refused to activate")

    dimension = int(env["EMBEDDING_DIMENSION"])
    adapter = HttpEmbeddingAdapter(
        base_url=env["EMBEDDING_BASE_URL"],
        api_key=env["EMBEDDING_API_KEY"],
        model=env["EMBEDDING_MODEL"],
        dimension=dimension,
    )
    payload = [
        {"ordinal": i, "content": c.content, "locator": c.locator}
        for i, c in enumerate(chunks)
    ]
    indexed = index_chunks(
        payload,
        embedding=adapter,
        expected_model=env["EMBEDDING_MODEL"],
        expected_dimension=dimension,
    )

    content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    source_id = uuid4()
    version_id = uuid4()
    fmt = path.suffix.lstrip(".").upper() or "MD"

    engine = create_engine(url, pool_pre_ping=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO knowledge_sources (id, type, title, status) "
                "VALUES (:id, :type, :title, 'ACTIVE')"
            ),
            {"id": source_id, "type": source_type, "title": title},
        )
        conn.execute(
            text(
                "INSERT INTO knowledge_versions ("
                "id, source_id, label, content_hash, format, parser_version, "
                "embedding_model, embedding_dimension, applicability_scope, "
                "product_versions, account_types, symptom_tags, status, is_active"
                ") VALUES ("
                ":id, :sid, 'v1', :hash, :fmt, 's2-parser-1', "
                ":model, :dim, 'GENERAL', '[]', '[]', '[]', 'READY', true"
                ")"
            ),
            {
                "id": version_id,
                "sid": source_id,
                "hash": content_hash,
                "fmt": fmt,
                "model": indexed.embedding_model,
                "dim": indexed.embedding_dimension,
            },
        )
        for i, chunk in enumerate(chunks):
            conn.execute(
                text(
                    "INSERT INTO knowledge_chunks ("
                    "id, version_id, ordinal, content, locator, embedding, lexical_text"
                    ") VALUES ("
                    ":id, :vid, :ord, :content, CAST(:locator AS jsonb), "
                    "CAST(:embedding AS jsonb), :lexical"
                    ")"
                ),
                {
                    "id": uuid4(),
                    "vid": version_id,
                    "ord": i,
                    "content": chunk.content,
                    "locator": json.dumps(chunk.locator, ensure_ascii=False),
                    "embedding": json.dumps(indexed.vectors[i]),
                    "lexical": chunk.content,
                },
            )

    return {
        "status": "READY",
        "source_id": str(source_id),
        "version_id": str(version_id),
        "chunk_count": len(chunks),
        "embedding_model": indexed.embedding_model,
        "embedding_dimension": indexed.embedding_dimension,
        "title": title,
        "provider": "dashscope-http",
    }


def disable_source_pg(source_id: str, *, database_url: str | None = None) -> None:
    env = require_real_embedding_env()
    url = database_url or env["DATABASE_URL"]
    engine = create_engine(url, pool_pre_ping=True)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE knowledge_sources SET status = 'DISABLED' WHERE id = CAST(:id AS uuid)"),
            {"id": source_id},
        )


def activate_switch_on_update(
    *,
    old_source_id: str,
    new_path: Path,
    title: str,
    source_type: str = "DOC",
) -> dict[str, Any]:
    """Disable old source candidates, import new file as new active source/version."""
    disable_source_pg(old_source_id)
    return persist_import_file(path=new_path, source_type=source_type, title=title)
