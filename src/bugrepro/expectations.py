"""The human expectation for a scenario.

An expectation is stored separately from the action sequence. It is only ever
read by the checker and copied into run records; nothing in the replay path is
allowed to derive, replace or weaken it.

Two kinds are supported, deliberately small:

``heading_text``
    A level-``N`` heading whose normalized text equals ``text``.

``region_text_exact``
    An accessible region named ``region`` contains a list item whose
    normalized text equals ``text`` exactly.

The ``level`` field is only meaningful for ``heading_text``. It stays in the
canonical form so that Phase 1/2 expectation fingerprints remain stable.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

HEADING_KIND = "heading_text"
REGION_KIND = "region_text_exact"
SUPPORTED_KINDS = frozenset({HEADING_KIND, REGION_KIND})
_MAX_TEXT_LENGTH = 200
_MAX_REGION_LENGTH = 80


class ExpectationError(ValueError):
    """Raised when an expectation is malformed."""


@dataclass(frozen=True)
class Expectation:
    kind: str
    text: str
    level: int = 1
    region: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "kind": self.kind,
            "text": self.text,
            "level": self.level,
        }
        if self.region is not None:
            data["region"] = self.region
        return data

    def fingerprint(self) -> str:
        """Stable digest of the expectation, included in every run record."""
        canonical = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_expectation(raw: Any) -> Expectation:
    if not isinstance(raw, dict):
        raise ExpectationError("expectation must be a JSON object.")
    unknown = set(raw) - {"kind", "text", "level", "region"}
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

    region = raw.get("region")
    if kind == REGION_KIND:
        if not isinstance(region, str) or not region.strip():
            raise ExpectationError(
                "expectation.region is required for region_text_exact."
            )
        if len(region) > _MAX_REGION_LENGTH:
            raise ExpectationError("expectation.region is too long.")
        if re.search(r"[\x00\r\n\t]", region):
            raise ExpectationError("expectation.region must be a single line.")
        region = region.strip()
    elif region is not None:
        raise ExpectationError(
            "expectation.region is only valid for region_text_exact."
        )

    return Expectation(kind=kind, text=text, level=level, region=region)


def normalize_heading(text: str) -> str:
    """Collapse insignificant whitespace, but preserve the words and case."""
    return re.sub(r"\s+", " ", text).strip()