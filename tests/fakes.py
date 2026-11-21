"""Deterministic resolver used only by orchestration tests."""

import json
from pathlib import Path

from git_ez.conflicts import Conflict
from git_ez.errors import EzError


class FakeResolver:
    def __init__(self, content="combined\n", confidence=0.95, action="write", error=None):
        self.content = content
        self.confidence = confidence
        self.action = action
        self.error = error
        self.calls: list[Conflict] = []

    def run(self, git, audit, round_number, paths, check):
        if self.error:
            raise EzError(self.error)
        for record in audit.manifest["conflicts"][-len(paths) :]:
            evidence = json.loads((audit.directory / record["artifacts"]["request"]).read_text())
            conflict = Conflict(**evidence)
            self.calls.append(conflict)
            path = Path(git.path) / conflict.path
            if self.action == "delete":
                path.unlink(missing_ok=True)
            else:
                path.write_text(self.content)
        return {"rationale": "Combine both intentions", "confidence": self.confidence}
