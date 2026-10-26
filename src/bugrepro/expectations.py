"""The human expectation for a scenario.

An expectation is stored separately from the action sequence. It is only ever
read by the checker and copied into run records; nothing in the replay path is
allowed to derive, replace or weaken it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

SUPPORTED_KINDS = frozenset({"heading_text"})
_MAX_TEXT_LENGTH = 200


class ExpectationError(ValueError):
    """Raised when an expectation is malformed."""


@dataclass(frozen=True)
class Expectation:
    kind: str
    text: str
    level: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "text": self.text, "level": self.level}

    def fingerprint(self) -> str:
        """Stable digest of the expectation, included in every run record."""
        canonical = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_expectation(raw: Any) -> Expectation:
    if not isinstance(raw, dict):
        raise ExpectationError("expectation must be a JSON object.")
    unknown = set(raw) - {"kind", "text", "level"}
    if unknown:
        raise ExpectationError(f"unknown expectation field(s): {sorted(unknown)}")

    kind = raw.get("kind")
    if kind not in SUPPORTED_KINDS:
        raise ExpectationError(
            f"expectation.kind must be one of {sorted(SUPPORTED_KINDS)}."
        )

    text = raw.get("text")
    if not isinstance(text, str):
        raise ExpectationError("expectation.text must be a string.")
    if not text or not text.strip():
        raise ExpectationError("expectation.text must not be empty.")
    if len(text) > _MAX_TEXT_LENGTH:
        raise ExpectationError("expectation.text is too long.")
    if re.search(r"[\x00\r\n]", text):
        raise ExpectationError("expectation.text must be a single line.")

    level = raw.get("level", 1)
    if isinstance(level, bool) or not isinstance(level, int) or not (1 <= level <= 6):
        raise ExpectationError("expectation.level must be an integer from 1 to 6.")

    return Expectation(kind=kind, text=text, level=level)


def normalize_heading(text: str) -> str:
    """Collapse insignificant whitespace, but preserve the words and case."""
    return re.sub(r"\s+", " ", text).strip()