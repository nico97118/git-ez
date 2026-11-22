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
    autosquash: bool | None = None

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
        if self.autosquash is True:
            flags += ["--interactive", "--autosquash"]
            if self.empty is None:
                # Interactive rebase otherwise changes the default to stop.
                flags.append("--empty=drop")
        elif self.autosquash is False:
            flags.append("--no-autosquash")
        return tuple(flags)


@dataclass(frozen=True)
class CommitOptions:
    message: str | None = None
    gpg_sign: str | Literal[False] | None = None
    signoff: bool | None = None
    verify_signatures: bool | None = None

    def __post_init__(self) -> None:
        if self.message is not None and "\0" in self.message:
            raise EzError("Commit message must not contain NUL characters")

    def flags(self, *, verify: bool = True) -> tuple[str, ...]:
        flags = []
        if self.message is not None:
            flags.append(f"--message={self.message}")
        if self.gpg_sign is False:
            flags.append("--no-gpg-sign")
        elif isinstance(self.gpg_sign, str):
            flags.append(f"--gpg-sign={self.gpg_sign}")
        if self.signoff is not None:
            flags.append("--signoff" if self.signoff else "--no-signoff")
        if verify and self.verify_signatures is not None:
            flags.append(
                "--verify-signatures" if self.verify_signatures else "--no-verify-signatures"
            )
        return tuple(flags)
