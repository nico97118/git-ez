# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Read-only Git observations and faithful bounded worktree evidence."""

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .audit_state import now, safe_error
from .audit_storage import regular_fd
from .errors import AuditError, EzError
from .git import Git

if TYPE_CHECKING:
    from .audit import Audit


def observe(git: Git) -> dict[str, Any]:
    observation: dict[str, Any] = {"id": uuid4().hex, "started_at": now(), "errors": []}
    for field, command in (
        ("head", ("rev-parse", "HEAD")),
        ("branch", ("symbolic-ref", "-q", "HEAD")),
        ("status", ("status", "--porcelain=v1", "--untracked-files=all")),
        ("unmerged", ("ls-files", "--unmerged", "-z")),
    ):
        try:
            result = git.run(*command, check=False)
            if result.returncode and not (field == "branch" and result.returncode == 1):
                observation["errors"].append(
                    {
                        "field": field,
                        "returncode": result.returncode,
                        "type": "GitObservationFailed",
                    }
                )
                observation[field] = None
            else:
                observation[field] = result.stdout.decode(
                    "utf-8", errors="backslashreplace"
                ).strip()
        except (OSError, EzError) as exc:
            observation[field] = None
            observation["errors"].append({"field": field, "type": type(exc).__name__})
    try:
        observation["operation_active"] = git.operation_active()
    except (OSError, EzError) as exc:
        observation["operation_active"] = None
        observation["errors"].append({"field": "operation", "type": type(exc).__name__})
    observation["finished_at"] = now()
    return observation


def capture_worktree(
    audit: "Audit",
    git: Git,
    directory: Path,
    limit: int,
    paths: list[str] | None = None,
    reference: str = "HEAD",
) -> None:
    namespace = directory.relative_to(audit.directory).as_posix()
    capture_id = uuid4().hex
    before = audit.record_observation(git)
    errors: list[dict[str, Any]] = []
    outputs: dict[str, bytes] = {}
    for label, command in (
        ("diff", ("diff", "--no-ext-diff", "--no-textconv", "--binary", reference, "--")),
        (
            "changed",
            ("diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", reference, "--"),
        ),
        ("untracked", ("ls-files", "--others", "--exclude-standard", "-z")),
    ):
        try:
            result = git.run(*command, check=False)
            outputs[label] = result.stdout
            if result.returncode:
                errors.append({"command": label, "returncode": result.returncode})
        except (OSError, EzError) as exc:
            outputs[label] = b""
            errors.append({"command": label, "type": type(exc).__name__})
    diff_error = audit.attempt(
        "worktree diff",
        lambda: audit.write(f"{namespace}/worktree.diff", outputs["diff"], capture_id=capture_id),
        f"{namespace}/worktree.diff",
    )
    names = set(
        (outputs["changed"] + outputs["untracked"])
        .decode("utf-8", errors="surrogateescape")
        .split("\0")
    ) | set(paths or [])
    records: list[dict[str, Any]] = []
    for name in sorted(names - {""}):
        record: dict[str, Any] = {"path": name}
        records.append(record)
        try:
            path = git.safe_path(name)
            try:
                with regular_fd(path) as descriptor:
                    initial = os.fstat(descriptor)
                    blocks = bytearray()
                    while len(blocks) <= limit:
                        block = os.read(descriptor, min(65536, limit + 1 - len(blocks)))
                        if not block:
                            break
                        blocks.extend(block)
                    final = os.fstat(descriptor)
            except FileNotFoundError:
                record["state"] = "deleted"
                continue
            if (initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns, initial.st_nlink) != (
                final.st_size,
                final.st_mtime_ns,
                final.st_ctime_ns,
                final.st_nlink,
            ):
                raise EzError("Source changed during audit capture")
            with regular_fd(path) as current:
                if (os.fstat(current).st_dev, os.fstat(current).st_ino) != (
                    initial.st_dev,
                    initial.st_ino,
                ):
                    raise EzError("Source changed during audit capture")
            content = bytes(blocks[:limit])
            artifact = f"files/{len(records):04d}.bin"
            relative = f"{namespace}/{artifact}"
            audit.write(
                relative,
                content,
                capture_id=capture_id,
                role="conflict_proposal" if name in (paths or []) else "file_content",
                retained_limit=limit,
            )
            if len(blocks) > limit:
                audit.limit_evidence(relative, "truncated", "File exceeds configured capture limit")
            evidence = audit.evidence[relative]
            record.update(
                state="file",
                artifact=artifact,
                size=initial.st_size,
                mode=initial.st_mode & 0o7777,
                recorded_bytes=evidence["retained_bytes"],
                truncated=len(blocks) > limit or evidence.get("truncated", False),
                redacted=evidence.get("redacted", False),
                recorded_sha256=evidence["sha256"],
            )
        except (OSError, EzError, ValueError) as exc:
            record.update(state="unavailable", error=safe_error(exc))
            errors.append({"path": name, "type": type(exc).__name__})
    after = audit.record_observation(git)
    observation_errors = before["errors"] + after["errors"]
    if (before["head"], before["branch"]) != (after["head"], after["branch"]):
        observation_errors.append({"field": "identity", "type": "GitStateChangedDuringCapture"})
    errors.extend(observation_errors)
    if diff_error is not None:
        errors.append({"command": "diff write", "type": type(diff_error).__name__})
    audit.write_json(
        f"{namespace}/files.json",
        {
            "capture_id": capture_id,
            "reference": reference,
            "head": after["head"],
            "branch": after["branch"],
            "before_observation": before["id"],
            "after_observation": after["id"],
            "files": records,
            "capture_errors": errors,
        },
        schema=True,
        capture_id=capture_id,
        role="worktree_inventory",
    )
    if any(record.get("redacted") for record in records):
        audit.limit_evidence(
            f"{namespace}/files.json", "redacted", "Known credentials removed from file captures"
        )
    if any(record.get("truncated") for record in records):
        audit.limit_evidence(
            f"{namespace}/files.json", "truncated", "File capture contains explicit prefixes"
        )
    if errors:
        audit.limit_evidence(
            f"{namespace}/files.json", "observation_error", "Worktree evidence is incomplete"
        )
        raise AuditError("Audit worktree capture failed; inspect capture errors")
