"""Investigation visibility / authorization status codes (SPEC §3/§4)."""

from __future__ import annotations


def investigation_access_status(
    *,
    viewer_user_id: str,
    owner_user_id: str,
    viewer_role: str,
    action: str,
) -> int:
    """Return HTTP status for a viewer acting on an investigation.

    - SUPPORT_AGENT may only read/operate own investigations; others are invisible (404).
    - KNOWLEDGE_ADMIN does not default-read support investigation bodies (403).
    """
    _ = action
    if viewer_role == "KNOWLEDGE_ADMIN":
        return 403
    if viewer_user_id != owner_user_id:
        return 404
    return 200
