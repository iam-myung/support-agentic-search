"""In-memory ConfigVersionService: register / activate / list; no secrets."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

SECRET_FIELD_NAMES = frozenset(
    {
        "api_key",
        "API_KEY",
        "llm_api_key",
        "embedding_api_key",
        "password",
        "secret",
        "token",
        "LLM_API_KEY",
        "EMBEDDING_API_KEY",
    }
)


class ConfigVersionError(ValueError):
    """Raised for OP-13 validation / activation / delete policy failures."""


@dataclass
class _Version:
    id: UUID
    kind: str
    content_hash: str
    payload: dict[str, Any]
    created_by: str
    is_active: bool = False
    referenced_by: set[str] = field(default_factory=set)


class ConfigVersionService:
    """Domain service for immutable config versions (SPEC §3 / OP-13)."""

    def __init__(self, *, config_root: Path | str) -> None:
        self._config_root = Path(config_root).resolve()
        self._versions: dict[UUID, _Version] = {}
        self._order: list[UUID] = []

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
            text = resolved.read_text(encoding="utf-8")
            body: dict[str, Any] = {"path": resolved.name, "text": text}
        elif payload is not None:
            body = dict(payload)
        else:
            raise ConfigVersionError("register requires path or payload")

        self._reject_secrets(body)
        content_hash = hashlib.sha256(
            json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

        vid = uuid4()
        self._versions[vid] = _Version(
            id=vid,
            kind=kind_norm,
            content_hash=content_hash,
            payload=body,
            created_by=created_by,
        )
        self._order.append(vid)
        return {"version_id": str(vid), "content_hash": content_hash, "kind": kind_norm}

    def activate(self, version_id: UUID | str) -> dict[str, Any]:
        vid = UUID(str(version_id))
        if vid not in self._versions:
            raise ConfigVersionError(f"unknown version_id={version_id}")
        target = self._versions[vid]
        for other in self._versions.values():
            if other.kind == target.kind and other.is_active:
                other.is_active = False
        target.is_active = True
        return self._row(target)

    def get_active(self, *, kind: str) -> dict[str, Any] | None:
        kind_norm = str(kind).strip().lower()
        for vid in reversed(self._order):
            row = self._versions[vid]
            if row.kind == kind_norm and row.is_active:
                return self._row(row)
        return None

    def list_versions(self, *, kind: str | None = None) -> list[dict[str, Any]]:
        kind_norm = str(kind).strip().lower() if kind is not None else None
        out: list[dict[str, Any]] = []
        for vid in self._order:
            row = self._versions[vid]
            if kind_norm is None or row.kind == kind_norm:
                out.append(self._row(row))
        return out

    def mark_referenced(self, version_id: UUID | str, *, investigation_id: str) -> None:
        vid = UUID(str(version_id))
        if vid not in self._versions:
            raise ConfigVersionError(f"unknown version_id={version_id}")
        self._versions[vid].referenced_by.add(str(investigation_id))

    def delete(self, version_id: UUID | str) -> None:
        vid = UUID(str(version_id))
        if vid not in self._versions:
            raise ConfigVersionError(f"unknown version_id={version_id}")
        row = self._versions[vid]
        if row.referenced_by:
            raise ConfigVersionError(
                f"cannot delete referenced config version {vid}; in use by investigations"
            )
        del self._versions[vid]
        self._order = [x for x in self._order if x != vid]

    def _row(self, row: _Version) -> dict[str, Any]:
        return {
            "version_id": str(row.id),
            "kind": row.kind,
            "content_hash": row.content_hash,
            "payload": dict(row.payload),
            "is_active": row.is_active,
            "created_by": row.created_by,
        }

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
