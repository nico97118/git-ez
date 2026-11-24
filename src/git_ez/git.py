# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Git plumbing with literal paths, pinned revisions, and no shell interpolation."""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .errors import EzError

# Preserve Git's configuration, identity, signing and transport settings. Only clear
# variables that could redirect operations away from the explicitly selected checkout.
REPOSITORY_ENV = {
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
}


def git_environment() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in REPOSITORY_ENV}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true"})
    return env


@dataclass(frozen=True)
class Stage:
    mode: str
    oid: str


class Git:
    def __init__(self, path: Path):
        self.path = path

    def run(
        self,
        *args: str,
        check: bool = True,
        sequence_editor: str | None = None,
        inherited_fds: tuple[int, ...] = (),
    ) -> subprocess.CompletedProcess[bytes]:
        environment = git_environment()
        if sequence_editor is not None:
            environment["GIT_SEQUENCE_EDITOR"] = sequence_editor
        result = subprocess.run(
            [
                "git",
                "--literal-pathspecs",
                "-c",
                "merge.conflictStyle=diff3",
                *args,
            ],
            cwd=self.path,
            env=environment,
            capture_output=True,
            check=False,
            pass_fds=inherited_fds,
        )
        if check and result.returncode:
            message = result.stderr.decode("utf-8", errors="replace").strip()
            raise EzError(f"Git {args[0]} failed: {message}")
        return result

    def text(self, *args: str) -> str:
        return self.run(*args).stdout.decode("utf-8").strip()

    def branch_oid(self, branch: str) -> str:
        if self.run("check-ref-format", "--branch", branch, check=False).returncode:
            raise EzError(f"Invalid branch name: {branch}")
        return self.text("rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}")

    def conflicts(self) -> dict[str, dict[int, Stage]]:
        conflicts: dict[str, dict[int, Stage]] = {}
        for entry in self.run("ls-files", "--unmerged", "-z").stdout.split(b"\0"):
            if not entry:
                continue
            header, path = entry.split(b"\t", 1)
            mode, oid, stage = header.decode("ascii").split()
            try:
                name = path.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise EzError("Non-UTF-8 filenames require manual resolution") from exc
            conflicts.setdefault(name, {})[int(stage)] = Stage(mode, oid)
        return conflicts

    def safe_path(self, name: str) -> Path:
        relative = PurePosixPath(name)
        if relative.is_absolute() or any(
            p == ".." or p.casefold() == ".git" for p in relative.parts
        ):
            raise EzError(f"Unsafe conflict path: {name}")
        candidate = self.path.joinpath(*relative.parts)
        for parent in (candidate, *candidate.parents):
            if parent == self.path:
                break
            if parent.is_symlink():
                raise EzError(f"Symlinks require manual resolution: {name}")
        if not candidate.resolve().is_relative_to(self.path.resolve()):
            raise EzError(f"Conflict path escapes worktree: {name}")
        return candidate

    def blob(self, stage: Stage, limit: int) -> bytes:
        if int(self.text("cat-file", "-s", stage.oid)) > limit:
            raise EzError("Conflict file exceeds configured size limit")
        return self.run("cat-file", "blob", stage.oid).stdout

    def _has_marker(self, name: str) -> bool:
        path = Path(self.text("rev-parse", "--path-format=absolute", "--git-path", name))
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        return True

    def merge_active(self) -> bool:
        return self._has_marker("MERGE_HEAD")

    def rebase_active(self) -> bool:
        return any(self._has_marker(name) for name in ("rebase-merge", "rebase-apply"))

    def rebase_metadata(self, backend: str, name: str) -> str:
        from .sequence_editor import read_regular

        path = Path(
            self.text("rev-parse", "--path-format=absolute", "--git-path", f"{backend}/{name}")
        )
        return read_regular(path, 4096).decode("utf-8").strip()

    def rebase_position(self) -> tuple[str, int, str] | None:
        """Identify a replay stop conservatively across both native backends."""
        from .sequence_editor import read_regular

        for backend, counter, commit in (
            ("rebase-merge", "msgnum", "stopped-sha"),
            ("rebase-apply", "next", "original-commit"),
        ):
            if not self._has_marker(backend):
                continue
            try:
                step = int(self.rebase_metadata(backend, counter))
                oid = self.commit_oid(self.rebase_metadata(backend, commit))
                if oid != self.commit_oid("REBASE_HEAD"):
                    return None
                if backend == "rebase-merge":
                    path = Path(
                        self.text(
                            "rev-parse", "--path-format=absolute", "--git-path", f"{backend}/done"
                        )
                    )
                    instruction = read_regular(path, 1_000_000).splitlines()[-1].split()
                    if instruction[0] not in (b"pick", b"reword", b"squash", b"fixup"):
                        return None
                    commit_index = (
                        2 if instruction[0] == b"fixup" and instruction[1] in (b"-C", b"-c") else 1
                    )
                    if not oid.startswith(instruction[commit_index].decode("ascii")):
                        return None
                return backend, step, oid
            except (EzError, OSError, ValueError, IndexError, UnicodeError):
                return None
        return None

    def continue_native(self, operation: str) -> int:
        """Let Git own the first continuation, including editors and diagnostics."""
        return subprocess.call(
            ["git", operation, "--continue"],
            cwd=self.path,
            env={key: value for key, value in os.environ.items() if key not in REPOSITORY_ENV},
        )

    def commit_oid(self, revision: str) -> str:
        return self.text("rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}")

    def operation_active(self) -> bool:
        return any(
            self._has_marker(name)
            for name in (
                "rebase-merge",
                "rebase-apply",
                "MERGE_HEAD",
                "CHERRY_PICK_HEAD",
                "REVERT_HEAD",
                "sequencer",
                "BISECT_START",
            )
        )
