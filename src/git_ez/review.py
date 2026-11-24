# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Explicit terminal review of captured proposals; never stages files."""

import json
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from .audit_report import safe_text
from .audit_safety import redact_bytes
from .audit_storage import read_regular
from .git import Git

if TYPE_CHECKING:
    from .audit import Audit
from .conflicts import Decision, text_diff
from .errors import EzError


def review_proposal(
    directory: Path,
    decision: Decision,
    secrets: tuple[str, ...] = (),
    *,
    validation_failed: bool = False,
    diagnostic: str | None = None,
) -> tuple[str, str | None]:
    def display(text: str) -> None:
        filtered = redact_bytes(text.encode("utf-8", errors="backslashreplace"), secrets).decode()
        print("\n".join(safe_text(line) for line in filtered.split("\n")))

    display(read_regular(directory / "worktree.diff").decode(errors="replace"))
    capture = json.loads(read_regular(directory / "files.json"))
    for record in capture["files"]:
        if record.get("truncated"):
            raise EzError("Incomplete proposal capture cannot be approved")
        display(f"File capture: {json.dumps(record, ensure_ascii=True)}")
        # Git diff omits untracked contents; show every captured file as well.
        if record.get("artifact"):
            relative = PurePosixPath(record["artifact"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Unsafe proposal path")
            content = read_regular(directory / relative).decode(errors="replace")
            display(
                text_diff(
                    None, content, "/dev/null", record["path"] + " (captured current content)"
                )
            )
    display(f"Capture errors: {capture['capture_errors']}")
    display(f"Agent rationale, reported checks and uncertainties: {decision.rationale}")
    print(f"Agent confidence: {decision.confidence}")
    if validation_failed:
        display("Check failed; this proposal cannot be accepted.")
        display(diagnostic or "Validation diagnostic unavailable")
    else:
        print("Proposal ready for review; final integration verification remains required.")
    try:
        while True:
            prompt = (
                "[r] Retry with feedback / [q] Quit: "
                if validation_failed
                else ("[a] Accept / [r] Reject / Reprompt / [q] Quit: ")
            )
            action = input(prompt).strip().lower()
            if action in ("a", "accept") and not validation_failed:
                return "accept", None
            if action in ("q", "quit", "s", "stop"):
                return "stop", None
            if action in ("r", "revise", "retry", "reject"):
                feedback = input("Revision feedback: ").strip()
                if feedback:
                    return "revise", feedback
            print("An explicit choice and nonempty revision feedback are required.")
    except (EOFError, OSError) as exc:
        raise EzError("Cannot read review input; proposal and Git operation preserved") from exc


def capture_proposal(
    audit: "Audit", git: Git, directory: Path, limit: int, paths: list[str]
) -> None:
    """Retain the selected proposal for user review, excluding untouched Git resources."""
    prefix = directory.relative_to(audit.directory).as_posix()
    diff = git.run(
        "diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD", "--", *paths
    ).stdout
    audit.write(f"{prefix}/worktree.diff", diff)
    records = []
    for number, name in enumerate(paths, 1):
        path = git.safe_path(name)
        record: dict[str, Any] = {"path": name}
        if not path.exists():
            record["state"] = "deleted"
        else:
            content = read_regular(path, limit + 1)
            relative = f"files/{number:04d}.bin"
            audit.write(f"{prefix}/{relative}", content[:limit])
            record.update(
                state="file",
                artifact=relative,
                size=path.stat().st_size,
                recorded_bytes=len(content[:limit]),
                truncated=len(content) > limit,
                mode=path.stat().st_mode,
            )
        records.append(record)
    audit.write_json(f"{prefix}/files.json", {"files": records, "capture_errors": []})
