"""Phase 1 prototype API bypass control for pilot environments."""

from __future__ import annotations


def unauthenticated_phase1_status(mode: str) -> int | None:
    """Return status to reject unauthenticated Phase 1 API, or None to allow.

    - open: local prototype may remain unauthenticated
    - disabled / local_only: pilot must not leave an open bypass (SPEC §4)
    """
    normalized = (mode or "open").strip().lower()
    if normalized == "open":
        return None
    if normalized in {"disabled", "local_only"}:
        return 401
    return 401
