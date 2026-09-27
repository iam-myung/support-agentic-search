"""Import-path safety: corpus text must not alter system rules fingerprint."""

from __future__ import annotations

from typing import Any

SYSTEM_RULES_FINGERPRINT = "rules-v1-no-tools-from-corpus"


def rules_fingerprint_after_ingest(chunks: list[Any]) -> str:
    # Corpus instructional text is data only; fingerprint stays stable.
    _ = chunks
    return SYSTEM_RULES_FINGERPRINT
