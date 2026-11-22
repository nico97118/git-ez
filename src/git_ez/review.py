# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Explicit terminal review of captured proposals; never stages files."""

import difflib
import json
from pathlib import Path

from .conflicts import Decision
from .errors import EzError


def review_proposal(directory: Path, decision: Decision) -> tuple[str, str | None]:
    print((directory / "worktree.diff").read_text(errors="replace"))
    capture = json.loads((directory / "files.json").read_text())
    for record in capture["files"]:
        print(f"File capture: {json.dumps(record, ensure_ascii=True)}")
        # Git diff omits untracked contents; show every captured file as well.
        if record.get("artifact"):
            content = (directory / record["artifact"]).read_text(errors="replace")
            print(
                "".join(
                    difflib.unified_diff(
                        [],
                        content.splitlines(keepends=True),
                        fromfile="/dev/null",
                        tofile=record["path"] + " (captured current content)",
                    )
                )
            )
    print(f"Capture errors: {capture['capture_errors']}")
    print(f"Agent rationale, reported checks and uncertainties: {decision.rationale}")
    print(f"Agent confidence: {decision.confidence}")
    print("Engine checks and final validation have not yet run for this acceptance.")
    try:
        while True:
            action = input("[a]ccept / [r]evise / [s]top: ").strip().lower()
            if action in ("a", "accept"):
                return "accept", None
            if action in ("s", "stop"):
                return "stop", None
            if action in ("r", "revise"):
                feedback = input("Revision feedback: ").strip()
                if feedback:
                    return "revise", feedback
            print("An explicit choice and nonempty revision feedback are required.")
    except (EOFError, OSError) as exc:
        raise EzError("Cannot read review input; proposal and Git operation preserved") from exc
