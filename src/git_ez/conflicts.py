# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Provider-neutral inputs and strictly validated model decisions."""

import math
import re
from dataclasses import asdict, dataclass
from typing import Any

from .errors import EzError

MARKERS = re.compile(r"^(?:<{7,}|={7,}|>{7,}|\|{7,})(?:.*)$", re.MULTILINE)


@dataclass(frozen=True)
class Conflict:
    path: str
    operation: str
    base: str | None
    ours: str | None
    theirs: str | None
    marked: str | None
    context: str

    def payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Decision:
    rationale: str
    confidence: float

    @classmethod
    def parse(cls, value: object) -> "Decision":
        if not isinstance(value, dict) or set(value) != {"rationale", "confidence"}:
            raise EzError("Decision must contain exactly rationale and confidence")
        rationale, confidence = value["rationale"], value["confidence"]
        if not isinstance(rationale, str) or not rationale.strip():
            raise EzError("The agent must explain its resolution")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            raise EzError("Confidence must be a finite number between 0 and 1")
        return cls(rationale, float(confidence))


class LowConfidence(EzError):
    """Preserve edited files without continuing Git."""
