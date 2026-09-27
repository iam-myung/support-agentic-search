"""Knowledge domain service: source/version activate & disable invariants."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4


class ActivationError(ValueError):
    """Raised when a version cannot be activated under SPEC §3 invariants."""


@dataclass
class _Version:
    id: UUID
    source_id: UUID
    label: str
    content_hash: str
    format: str
    status: str
    is_active: bool = False
    is_empty: bool = False


@dataclass
class _Source:
    id: UUID
    type: str
    title: str
    status: str = "ACTIVE"
    case_ref: str | None = None
    confirmed_resolution: bool | None = None


@dataclass
class CandidateRecord:
    source_id: UUID
    version_id: UUID


class KnowledgeService:
    """In-memory knowledge ownership for S1 activate/disable transitions.

    Persistence via SQLAlchemy/Alembic schema is separate; this service encodes
    domain invariants that the repository must uphold.
    """

    def __init__(self) -> None:
        self._sources: dict[UUID, _Source] = {}
        self._versions: dict[UUID, _Version] = {}

    def create_source(
        self,
        *,
        type: str,
        title: str,
        case_ref: str | None = None,
        confirmed_resolution: bool | None = None,
    ) -> UUID:
        source_id = uuid4()
        self._sources[source_id] = _Source(
            id=source_id,
            type=type,
            title=title,
            case_ref=case_ref,
            confirmed_resolution=confirmed_resolution,
        )
        return source_id

    def add_version(
        self,
        *,
        source_id: UUID,
        label: str,
        content_hash: str,
        format: str,
        status: str,
        is_empty: bool = False,
    ) -> UUID:
        if source_id not in self._sources:
            raise KeyError(f"unknown source_id={source_id}")
        version_id = uuid4()
        self._versions[version_id] = _Version(
            id=version_id,
            source_id=source_id,
            label=label,
            content_hash=content_hash,
            format=format,
            status=status,
            is_empty=is_empty,
        )
        return version_id

    def activate_version(self, version_id: UUID) -> None:
        version = self._versions[version_id]
        if version.status == "FAILED":
            raise ActivationError("cannot activate FAILED version")
        if version.is_empty:
            raise ActivationError("cannot activate empty content version")
        if version.status not in {"READY", "PROCESSING"}:
            raise ActivationError(f"cannot activate version in status={version.status}")

        for other in self._versions.values():
            if other.source_id == version.source_id and other.is_active:
                other.is_active = False

        version.status = "READY"
        version.is_active = True

    def disable_source(self, source_id: UUID) -> None:
        self._sources[source_id].status = "DISABLED"

    def list_active_ready_versions(self, *, source_id: UUID) -> list[UUID]:
        return [
            v.id
            for v in self._versions.values()
            if v.source_id == source_id and v.is_active and v.status == "READY"
        ]

    def list_investigation_candidates(self) -> list[UUID]:
        return [
            v.id
            for v in self._versions.values()
            if v.is_active
            and v.status == "READY"
            and self._sources[v.source_id].status == "ACTIVE"
        ]

    def list_candidate_records(self) -> list[CandidateRecord]:
        return [
            CandidateRecord(source_id=v.source_id, version_id=v.id)
            for v in self._versions.values()
            if v.is_active
            and v.status == "READY"
            and self._sources[v.source_id].status == "ACTIVE"
        ]
