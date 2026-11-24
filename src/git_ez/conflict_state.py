# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Private, short-lived snapshots of a native conflict stop (never a Git stash)."""

import hashlib
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .errors import EzError
from .git import Git, git_environment


@contextmanager
def uninterrupted() -> Iterator[None]:
    """Finish cleanup even if SIGINT is repeated."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def remove(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path)


def reconcile(source: Path, destination: Path) -> None:
    """Restore Git operation metadata without following destination links."""
    for path in destination.iterdir():
        if not (source / path.name).exists() and not (source / path.name).is_symlink():
            remove(path)
    for original in source.iterdir():
        target = destination / original.name
        if original.is_symlink():
            remove(target)
            target.symlink_to(os.readlink(original))
        elif original.is_dir():
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                remove(target)
            target.mkdir(exist_ok=True)
            reconcile(original, target)
            shutil.copystat(original, target)
        else:
            if target.is_symlink() or target.is_dir():
                remove(target)
            shutil.copy2(original, target)


@dataclass(frozen=True)
class SavedFile:
    mode: int
    content: str
    mtime_ns: int
    atime_ns: int
    artifact: Path | None = None


class ConflictState:
    """Preserve the original index paths, reusing Git blobs for unchanged contents.

    Nontracked paths are never traversed or removed. Only tracked contents absent
    from their stage-zero blob require a private file copy.
    """

    def __init__(self, git: Git):
        self.git = git
        self.directory = Path(tempfile.mkdtemp(prefix="git-ez-conflict-"))
        self.retained = False
        self.index = Path(git.text("rev-parse", "--path-format=absolute", "--git-path", "index"))
        try:
            self.object_format = git.text("rev-parse", "--show-object-format")
            self.files: dict[str, SavedFile | None] = {}
            indexed: dict[str, str | None] = {}
            self.index_entries = git.run("ls-files", "--stage", "-z").stdout
            for entry in self.index_entries.split(b"\0"):
                if not entry:
                    continue
                header, raw_name = entry.split(b"\t", 1)
                mode, oid, stage = header.decode("ascii").split()
                name = raw_name.decode("utf-8")
                if mode not in ("100644", "100755", "120000"):
                    raise EzError(f"Unsupported tracked file in interactive snapshot: {name}")
                indexed[name] = oid if stage == "0" else None
            for name, indexed_oid in indexed.items():
                path = self._path(name)
                if not path.exists() and not path.is_symlink():
                    self.files[name] = None
                    continue
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode):
                    self.files[name] = SavedFile(
                        info.st_mode, os.readlink(path), info.st_mtime_ns, info.st_atime_ns
                    )
                elif stat.S_ISREG(info.st_mode):
                    content = self._blob_oid(path)
                    artifact = None
                    if content != indexed_oid:
                        artifact = self.directory / "worktree" / name
                        artifact.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, artifact)
                    self.files[name] = SavedFile(
                        info.st_mode, content, info.st_mtime_ns, info.st_atime_ns, artifact
                    )
                else:
                    raise EzError(f"Cannot snapshot nonregular tracked file: {name}")
            shutil.copy2(self.index, self.directory / "index")
            shared = git.text("rev-parse", "--shared-index-path")
            self.shared_index = (git.path / shared).resolve() if shared else None
            if self.shared_index is not None:
                shutil.copy2(self.shared_index, self.directory / "shared-index")
            self.metadata = Path(git.text("rev-parse", "--absolute-git-dir"))
            self.control_names = (
                "rebase-merge",
                "rebase-apply",
                "MERGE_HEAD",
                "MERGE_MSG",
                "MERGE_MODE",
                "MERGE_AUTOSTASH",
                "AUTO_MERGE",
                "REBASE_HEAD",
                "CHERRY_PICK_HEAD",
                "index.lock",
            )
            control = self.directory / "control"
            control.mkdir()
            for name in self.control_names:
                source = self.metadata / name
                if source.is_dir():
                    shutil.copytree(source, control / name, symlinks=True)
                elif source.exists():
                    shutil.copy2(source, control / name, follow_symlinks=False)
        except BaseException:
            shutil.rmtree(self.directory)
            raise

    def _path(self, name: str) -> Path:
        relative = Path(name)
        if relative.name.casefold() == ".git":
            raise EzError(f"Unsafe tracked path: {name}")
        # safe_path rejects symlink parents; the leaf itself may be a tracked link.
        return self.git.safe_path(relative.parent.as_posix()) / relative.name

    def _blob_oid(self, path: Path) -> str:
        digest = hashlib.new(self.object_format)
        digest.update(f"blob {path.stat().st_size}\0".encode("ascii"))
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def _fingerprint(self, name: str) -> tuple[int, str] | None:
        path = self._path(name)
        if not path.exists() and not path.is_symlink():
            return None
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            return mode, os.readlink(path)
        if stat.S_ISREG(mode):
            return mode, self._blob_oid(path)
        return mode, "unsupported"

    def changed_paths(self) -> list[str]:
        return sorted(
            name
            for name, saved in self.files.items()
            if self._fingerprint(name) != (None if saved is None else (saved.mode, saved.content))
        )

    def _restore_files(self) -> None:
        for name, saved in self.files.items():
            if self._fingerprint(name) == (None if saved is None else (saved.mode, saved.content)):
                continue
            path = self._path(name)
            if path.is_dir() and not path.is_symlink():
                # Never recursively remove nontracked files placed at a tracked path.
                path.rmdir()
            if saved is None:
                path.unlink(missing_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            if stat.S_ISLNK(saved.mode):
                path.unlink(missing_ok=True)
                path.symlink_to(saved.content)
                continue
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".git-ez-restore-", dir=path.parent
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    if saved.artifact is None:
                        result = subprocess.run(
                            ("git", "cat-file", "blob", saved.content),
                            cwd=self.git.path,
                            env=git_environment(),
                            stdout=stream,
                            stderr=subprocess.PIPE,
                            check=False,
                        )
                        if result.returncode:
                            raise EzError(
                                "Cannot restore saved Git blob: "
                                + result.stderr.decode(errors="replace").strip()
                            )
                    else:
                        with saved.artifact.open("rb") as source:
                            shutil.copyfileobj(source, stream)
                temporary.chmod(stat.S_IMODE(saved.mode))
                os.utime(temporary, ns=(saved.atime_ns, saved.mtime_ns))
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)

    def verify_control(self) -> None:
        def contents(path: Path) -> object:
            if path.is_symlink():
                return ("symlink", os.readlink(path))
            if path.is_dir():
                return {child.name: contents(child) for child in path.iterdir()}
            return path.read_bytes() if path.exists() else None

        for name in self.control_names:
            if contents(self.directory / "control" / name) != contents(self.metadata / name):
                raise EzError(f"Git operation metadata changed before approval: {name}")

    def restore(self) -> None:
        try:
            with uninterrupted():
                self._restore_files()
                for name in self.control_names:
                    saved = self.directory / "control" / name
                    target = self.metadata / name
                    if saved.is_dir():
                        if target.is_symlink() or (target.exists() and not target.is_dir()):
                            remove(target)
                        target.mkdir(exist_ok=True)
                        reconcile(saved, target)
                    elif saved.exists():
                        if target.is_symlink() or target.is_dir():
                            remove(target)
                        shutil.copy2(saved, target)
                    else:
                        remove(target)
                # Restore the dependency before publishing an index that references it.
                if self.shared_index is not None:
                    self._restore_index_file("shared-index", self.shared_index)
                self._restore_index_file("index", self.index)
                if self.git.run("ls-files", "--stage", "-z").stdout != self.index_entries:
                    raise EzError("Restored Git index entries differ from the conflict snapshot")
        except Exception as exc:
            # Keep the private backup for manual recovery if restoration fails.
            self.retained = True
            raise EzError(
                f"Conflict restoration failed; backup retained at {self.directory}: {exc}"
            ) from exc

    def _restore_index_file(self, saved: str, target: Path) -> None:
        descriptor, name = tempfile.mkstemp(prefix="index.ez-", dir=target.parent)
        os.close(descriptor)
        temporary = Path(name)
        try:
            shutil.copy2(self.directory / saved, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def close(self) -> None:
        with uninterrupted():
            shutil.rmtree(self.directory)
