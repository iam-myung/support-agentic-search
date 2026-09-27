"""PostgreSQL-backed config versions for OP-13 (real-path SMOKE)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from support_platform.config_mgmt.service import SECRET_FIELD_NAMES, ConfigVersionError


class PgConfigVersionService:
    """Persist immutable config versions to PostgreSQL ``config_versions``."""

    def __init__(self, *, database_url: str, config_root: Path | str) -> None:
        self._engine: Engine = create_engine(database_url, pool_pre_ping=True)
        self._config_root = Path(config_root).resolve()

    def register(
        self,
        *,
        kind: str,
        created_by: str,
        path: Path | str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        kind_norm = str(kind).strip().lower()
        if kind_norm not in {"prompt", "model", "retrieval"}:
            raise ConfigVersionError(f"unsupported kind={kind}")

        if path is not None:
            resolved = Path(path).resolve()
            try:
                resolved.relative_to(self._config_root)
            except ValueError as exc:
                raise ConfigVersionError(
                    f"path outside config_root: {resolved} not under {self._config_root}"
                ) from exc
            text_body = resolved.read_text(encoding="utf-8")
            body: dict[str, Any] = {"path": resolved.name, "text": text_body}
        elif payload is not None:
            body = dict(payload)
        else:
            raise ConfigVersionError("register requires path or payload")

        self._reject_secrets(body)
        content_hash = hashlib.sha256(
            json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        vid = uuid4()
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO config_versions
                      (id, kind, content_hash, payload, is_active, created_by)
                    VALUES
                      (CAST(:id AS uuid), :kind, :hash, CAST(:payload AS jsonb), false, :by)
                    """
                ),
                {
                    "id": str(vid),
                    "kind": kind_norm,
                    "hash": content_hash,
                    "payload": json.dumps(body, ensure_ascii=False),
                    "by": created_by,
                },
            )
        return {"version_id": str(vid), "content_hash": content_hash, "kind": kind_norm}

    def activate(self, version_id: UUID | str) -> dict[str, Any]:
        vid = str(version_id)
        with self._engine.begin() as conn:
            row = conn.execute(
                text(
                    "SELECT id::text AS id, kind FROM config_versions "
                    "WHERE id = CAST(:id AS uuid)"
                ),
                {"id": vid},
            ).mappings().one_or_none()
            if row is None:
                raise ConfigVersionError(f"unknown version_id={version_id}")
            conn.execute(
                text(
                    "UPDATE config_versions SET is_active = false "
                    "WHERE kind = :kind AND is_active = true"
                ),
                {"kind": row["kind"]},
            )
            conn.execute(
                text(
                    "UPDATE config_versions SET is_active = true "
                    "WHERE id = CAST(:id AS uuid)"
                ),
                {"id": vid},
            )
        active = self.get_active(kind=row["kind"])
        assert active is not None
        return active

    def get_active(self, *, kind: str) -> dict[str, Any] | None:
        kind_norm = str(kind).strip().lower()
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT id::text AS version_id, kind, content_hash, payload, is_active, created_by
                    FROM config_versions
                    WHERE kind = :kind AND is_active = true
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"kind": kind_norm},
            ).mappings().one_or_none()
        if row is None:
            return None
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return {
            "version_id": row["version_id"],
            "kind": row["kind"],
            "content_hash": row["content_hash"],
            "payload": payload,
            "is_active": bool(row["is_active"]),
            "created_by": row["created_by"],
        }

    def list_versions(self, *, kind: str | None = None) -> list[dict[str, Any]]:
        kind_norm = str(kind).strip().lower() if kind is not None else None
        with self._engine.connect() as conn:
            if kind_norm:
                rows = conn.execute(
                    text(
                        """
                        SELECT id::text AS version_id, kind, content_hash, payload,
                               is_active, created_by, created_at
                        FROM config_versions WHERE kind = :kind
                        ORDER BY created_at ASC
                        """
                    ),
                    {"kind": kind_norm},
                ).mappings().all()
            else:
                rows = conn.execute(
                    text(
                        """
                        SELECT id::text AS version_id, kind, content_hash, payload,
                               is_active, created_by, created_at
                        FROM config_versions
                        ORDER BY created_at ASC
                        """
                    )
                ).mappings().all()
        out: list[dict[str, Any]] = []
        for row in rows:
            payload = row["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            out.append(
                {
                    "version_id": row["version_id"],
                    "kind": row["kind"],
                    "content_hash": row["content_hash"],
                    "payload": payload,
                    "is_active": bool(row["is_active"]),
                    "created_by": row["created_by"],
                }
            )
        return out

    def mark_referenced(self, version_id: UUID | str, *, investigation_id: str) -> None:
        # Reference is implied by investigations.*_version columns; no-op for PG.
        _ = (version_id, investigation_id)

    def delete(self, version_id: UUID | str) -> None:
        vid = str(version_id)
        with self._engine.begin() as conn:
            refs = conn.execute(
                text(
                    """
                    SELECT 1 FROM investigations
                    WHERE prompt_version = :vid
                       OR model_config_version = :vid
                       OR retrieval_config_version = :vid
                    LIMIT 1
                    """
                ),
                {"vid": vid},
            ).fetchone()
            if refs is not None:
                raise ConfigVersionError(
                    f"cannot delete referenced config version {vid}; in use by investigations"
                )
            result = conn.execute(
                text("DELETE FROM config_versions WHERE id = CAST(:id AS uuid)"),
                {"id": vid},
            )
            if result.rowcount == 0:
                raise ConfigVersionError(f"unknown version_id={version_id}")

    @staticmethod
    def _reject_secrets(payload: dict[str, Any]) -> None:
        for key in payload:
            if key in SECRET_FIELD_NAMES or str(key).lower() in {
                "api_key",
                "password",
                "secret",
                "token",
                "llm_api_key",
                "embedding_api_key",
            }:
                raise ConfigVersionError(
                    f"secret field '{key}' is not allowed in config versions"
                )
