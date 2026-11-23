# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Checks owned by Git orchestration, independent of the external agent harness."""

import hashlib
import stat

from .audit import Audit
from .config import Config
from .conflicts import MARKERS
from .errors import EzError
from .git import Git


class GitStateChanged(EzError):
    """An agent or concurrent actor changed state owned by the orchestrator."""


class WorktreeChecks:
    def __init__(self, config: Config):
        self.config = config

    def guard(self, git: Git, audit: Audit, head: str, branch: str | None) -> None:
        changed = git.text("rev-parse", "HEAD") != head
        if branch is not None:
            changed |= git.text("symbolic-ref", "-q", "HEAD") != branch
        else:
            changed |= bool(git.run("symbolic-ref", "-q", "HEAD", check=False).stdout)
        inputs = audit.inputs
        changed |= git.branch_oid(inputs.branch) != inputs.before_oid
        for pinned in (inputs.target, inputs.onto):
            if pinned is not None and pinned.reference:
                changed |= git.commit_oid(pinned.reference) != pinned.oid
        if audit.operation == "rebase":
            changed |= not git.rebase_active()
        else:
            merge_head = git.run("rev-parse", "--verify", "MERGE_HEAD", check=False)
            changed |= merge_head.returncode != 0 or (
                merge_head.stdout.decode().strip() != inputs.target.oid
            )
        if changed:
            raise GitStateChanged(
                "Agent changed Git control state or input refs; integration stopped"
            )

    def _changed_paths(self, git: Git) -> set[str]:
        raw = git.run(
            "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", "HEAD", "--"
        ).stdout
        raw += git.run("ls-files", "--others", "--exclude-standard", "-z").stdout
        try:
            return set(raw.decode("utf-8").split("\0")) - {""}
        except UnicodeDecodeError as exc:
            raise EzError("Non-UTF-8 changed filenames require manual resolution") from exc

    def _fingerprint(self, git: Git, name: str) -> tuple[int, str] | None:
        path = git.safe_path(name)
        if not path.exists():
            return None
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise EzError(f"Changed file is not regular text: {name}")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        return stat.S_IMODE(info.st_mode), digest

    def baseline(self, git: Git) -> dict[str, tuple[int, str] | None]:
        """Fingerprint Git's pending changes before the resolver can edit them."""
        baseline = {}
        for name in self._changed_paths(git):
            try:
                baseline[name] = self._fingerprint(git, name)
            except EzError:
                # Unsupported paths remain subject to the existing verification rules.
                continue
        return baseline

    def verify(
        self,
        git: Git,
        conflicts: list[str],
        baseline: dict[str, tuple[int, str] | None],
    ) -> None:
        names = self._changed_paths(git) | set(conflicts) | baseline.keys()
        for name in names:
            if (
                name not in conflicts
                and name in baseline
                and self._fingerprint(git, name) == baseline[name]
            ):
                continue
            path = git.safe_path(name)
            if not path.exists():
                continue
            if not path.is_file() or path.stat().st_size > self.config.max_file_bytes:
                raise EzError(f"Changed file is not regular text or exceeds size limit: {name}")
            try:
                text = path.read_bytes().decode("utf-8")
            except UnicodeDecodeError as exc:
                raise EzError(f"Non-UTF-8 proposal requires manual resolution: {name}") from exc
            if "\0" in text or MARKERS.search(text):
                raise EzError(f"Binary content or conflict markers remain in {name}")
