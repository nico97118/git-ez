# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Resolution attempts and saved reports remain understandable without native history."""

import json

import pytest

from git_ez.audit import load_report
from git_ez.engine import Engine
from git_ez.errors import EzError
from tests.test_engine import repo as repo

pytestmark = pytest.mark.integration


def test_reprompt_keeps_both_proposals_and_common_reference(repo, monkeypatch):
    number = 0

    def resolve(git, audit, attempt, paths, check):
        nonlocal number
        number += 1
        repo.file.write_text(f"proposal {number}\n")
        return {"rationale": f"Explanation {number}", "confidence": 0.9 + number / 100}

    repo.resolver.run = resolve
    answers = iter(["r", "Keep error handling", "a"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", "merge", interactive=True)
    assert audit.view["status"] == "succeeded", audit.view
    attempts = json.loads((audit.directory / "run.json").read_text())["reviews"]
    assert len(attempts) == 2
    assert attempts[0]["decision"] == "revise"
    assert attempts[0]["application"] == "not_staged"
    assert attempts[1]["application"] == "staged"
    assert attempts[1]["feedback"] == "Keep error handling"
    for index, attempt in enumerate(attempts, 1):
        record = attempt["conflicts"][0]
        assert record["resolution"]["content"] == f"proposal {index}\n"
        assert record["resolution"]["rationale"] == f"Explanation {index}"
        diff = (audit.directory / record["artifacts"]["diff"]).read_text()
        assert "-source" in diff
        assert f"+proposal {index}" in diff
    report = load_report(audit.directory)
    assert "Attempt 1: revise" in report
    assert "Attempt 2: accept" in report
    assert "+proposal 1" in report and "+proposal 2" in report
    assert repo.git.text("show", "HEAD:file.txt") == "proposal 2"


@pytest.mark.parametrize("ending", ["error", "stop"])
def test_reprompt_failure_does_not_apply_previous_attempt(repo, monkeypatch, ending):
    number = 0

    def resolve(*args):
        nonlocal number
        number += 1
        repo.file.write_text(f"proposal {number}\n")
        if number == 2 and ending == "error":
            raise EzError("Resolver refused the revision")
        return {"rationale": f"Explanation {number}", "confidence": 0.95}

    repo.resolver.run = resolve
    answers = iter(["r", "Change this", "s"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", "merge", interactive=True)
    assert audit.view["status"] == "failed"
    first, second = audit.view["reviews"]
    assert first["decision"] == "revise"
    assert first["conflicts"][0]["resolution"]["content"] == "proposal 1\n"
    assert second["conflicts"][0]["resolution"]["content"] == "proposal 2\n"
    assert all(attempt["application"] == "not_staged" for attempt in (first, second))
    if ending == "error":
        assert "rationale" not in second["conflicts"][0]["resolution"]
    assert repo.git.conflicts()
    assert repo.git.branch_oid("feature") == repo.source_oid
