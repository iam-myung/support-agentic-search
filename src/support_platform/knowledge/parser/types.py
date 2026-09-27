"""Shared parse errors and chunk DTO."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ParseError(ValueError):
    """Raised when a corpus file cannot be parsed into citable chunks."""


@dataclass
class ParsedChunk:
    content: str
    locator: dict[str, Any]
