# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Explicitly supported native Git options."""

from dataclasses import dataclass
from typing import Literal

from .errors import EzError


@dataclass(frozen=True)
class RebaseOptions:
    empty: Literal["drop", "keep", "stop"] | None = None
    keep_empty: bool | None = None
    reapply_cherry_picks: bool | None = None
    force_rebase: bool | None = None

    def __post_init__(self) -> None:
        if self.empty is not None and self.empty not in ("drop", "keep", "stop"):
            raise EzError("Rebase empty policy must be drop, keep or stop")

    def flags(self) -> tuple[str, ...]:
        flags = []
        if self.empty is not None:
            flags.append(f"--empty={self.empty}")
        for name, value in (
            ("keep-empty", self.keep_empty),
            ("reapply-cherry-picks", self.reapply_cherry_picks),
        ):
            if value is not None:
                flags.append(f"--{'' if value else 'no-'}{name}")
        if self.force_rebase is not None:
            flags.append("--force-rebase" if self.force_rebase else "--ff")
        return tuple(flags)
