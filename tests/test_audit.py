# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Conflict decisions remain inspectable without duplicating Git history."""

import json
import os
from dataclasses import replace

import pytest

from git_ez.audit import Audit, load_report
from git_ez.audit_storage import publish, read_regular
from git_ez.engine import Engine
from git_ez.errors import EzError


@pytest.mark.integration
@pytest.mark.parametrize("operation", ["merge", "rebase"])
def test_each_conflict_retains_diff_resolution_rationale_and_confidence(repo, operation):
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", operation)
    assert audit.view["status"] == "succeeded", audit.view.get("error")
    data = json.loads((audit.directory / "run.json").read_bytes())
    record = data["conflicts"][0]
    assert record["application"] == "staged"
    assert record["resolution"] == {
        "action": "write",
        "content": "combined\n",
        "rationale": "Combine both intentions",
        "confidence": 0.95,
    }
    assert "source" in read_regular(audit.directory / record["artifacts"]["conflict_diff"]).decode()
    assert "parent" in read_regular(audit.directory / record["artifacts"]["conflict_diff"]).decode()
    assert "+combined" in read_regular(audit.directory / record["artifacts"]["diff"]).decode()
    assert read_regular(audit.directory / record["artifacts"]["proposed"]) == b"combined\n"
    assert repo.git.text("show", "HEAD:file.txt") == "combined"
    for name in (
        "git-steps",
        "integration.log",
        "result.diff",
        "range-diff.txt",
        "exit",
        "rebase-commits.log",
    ):
        assert not (audit.directory / name).exists()
    assert not ({"requirements", "artifacts", "git", "revision", "audit_health"} & data.keys())


@pytest.mark.integration
@pytest.mark.parametrize("action,content", [("delete", None), ("write", "")])
def test_deletion_and_empty_resolution_are_distinct(repo, action, content):
    repo.resolver.action = action
    repo.resolver.content = content
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", "merge")
    assert audit.view["status"] == "succeeded", audit.view.get("error")
    resolution = audit.view["conflicts"][0]["resolution"]
    assert resolution["action"] == action
    assert resolution["content"] == content


@pytest.mark.integration
def test_low_confidence_retains_proposal_and_reason_without_staging(repo):
    repo.resolver.confidence = 0.1
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", "merge")
    record = audit.view["conflicts"][0]
    assert record["status"] == "low_confidence"
    assert record["resolution"]["content"] == "combined\n"
    assert record["resolution"]["rationale"] == "Combine both intentions"
    assert record["resolution"]["confidence"] == 0.1
    assert repo.git.conflicts()
    assert repo.git.branch_oid("feature") == repo.source_oid


@pytest.mark.integration
def test_multiple_conflicts_keep_separate_diffs_and_resolutions(repo):
    other = repo.root / "other.txt"
    other.write_text("source other\n")
    repo.git.run("add", ".")
    repo.git.run("commit", "-m", "source other")
    repo.git.run("checkout", "main")
    other.write_text("target other\n")
    repo.git.run("add", ".")
    repo.git.run("commit", "-m", "target other")
    repo.git.run("checkout", "feature")
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", "merge")
    assert audit.view["status"] == "succeeded", audit.view.get("error")
    records = audit.view["conflicts"]
    assert {record["path"] for record in records} == {"file.txt", "other.txt"}
    assert len({record["artifacts"]["diff"] for record in records}) == 2
    assert all(record["resolution"]["confidence"] == 0.95 for record in records)


@pytest.mark.integration
def test_missing_resolution_capture_stops_before_staging(repo, monkeypatch):
    import git_ez.audit as module

    original = module.publish

    def fail(path, content, **kwargs):
        if path.name == "resolution.diff":
            raise OSError("synthetic write failure")
        return original(path, content, **kwargs)

    monkeypatch.setattr(module, "publish", fail)
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", "merge")
    assert audit.view["status"] == "failed"
    assert repo.git.conflicts()
    assert repo.git.operation_active()
    assert repo.git.branch_oid("feature") == repo.source_oid
    assert audit.view["conflicts"][0]["capture_error"]


@pytest.mark.integration
def test_journal_failure_after_integration_preserves_the_local_result(repo, monkeypatch):
    import git_ez.audit as module

    original = module.write_json

    def fail(path, value):
        if value.get("status") == "succeeded":
            raise OSError("synthetic final write failure")
        return original(path, value)

    monkeypatch.setattr(module, "write_json", fail)
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", "merge")
    assert audit.view["integrated"]
    assert audit.view["status"] == "failed"
    assert not audit.published
    assert repo.git.branch_oid("feature") == audit.view["result_oid"]
    assert not repo.git.operation_active()


@pytest.mark.integration
def test_configured_credentials_are_filtered_from_decisions(repo):
    secret = 'synthetic-"credential'
    config = replace(repo.config, api_key=secret)
    original = repo.resolver.run

    def resolve(*args):
        decision = original(*args)
        decision["rationale"] = "Resolved " + secret
        return decision

    repo.resolver.run = resolve
    audit = Engine(repo.root, config, repo.resolver).run("main", "merge")
    stored = json.loads((audit.directory / "run.json").read_bytes())
    assert stored["conflicts"][0]["resolution"]["rationale"] == "Resolved <redacted>"
    assert secret not in load_report(audit.directory)


@pytest.mark.integration
def test_show_uses_saved_decisions_without_git_or_cached_report(repo, monkeypatch):
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", "merge")
    (audit.directory / "report.md").write_text("# forged cached result")
    monkeypatch.setattr("git_ez.git.Git.run", lambda *a, **k: pytest.fail("Reader called Git"))
    report = load_report(audit.directory)
    assert "Combine both intentions" in report
    assert "Confidence: 0.95" in report
    assert "forged" not in report


@pytest.mark.unit
def test_constructor_is_side_effect_free_and_collision_does_not_overwrite(tmp_path):
    directory = tmp_path / "run"
    audit = Audit(directory, {"operation": "merge", "branch": "feature", "conflicts": []})
    assert not directory.exists()
    directory.mkdir()
    (directory / "existing").write_text("keep")
    with pytest.raises(FileExistsError):
        audit.initialize()
    assert (directory / "existing").read_text() == "keep"


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_reading_rejects_unsafe_files(tmp_path, kind):
    source = tmp_path / "source"
    if kind == "symlink":
        outside = tmp_path / "outside"
        outside.write_bytes(b"private")
        source.symlink_to(outside)
    else:
        os.mkfifo(source)
    with pytest.raises((OSError, ValueError)):
        read_regular(source)


@pytest.mark.unit
def test_failed_atomic_write_preserves_previous_file(tmp_path, monkeypatch):
    target = tmp_path / "run.json"
    publish(target, b"old", replace=True)
    monkeypatch.setattr(os, "write", lambda *args: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(OSError):
        publish(target, b"new", replace=True)
    assert target.read_bytes() == b"old"


@pytest.mark.unit
def test_unreadable_journal_never_trusts_markdown_cache(tmp_path):
    (tmp_path / "report.md").write_text("succeeded")
    with pytest.raises(EzError, match="Cannot read resolution journal"):
        load_report(tmp_path)


@pytest.mark.integration
def test_interruption_does_not_invent_a_model_explanation_or_confidence(repo):
    def interrupt(*args):
        repo.file.write_text("partial proposal\n")
        raise KeyboardInterrupt

    repo.resolver.run = interrupt
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", "merge")
    record = json.loads((audit.directory / "run.json").read_text())["conflicts"][0]
    assert record["status"] == "interrupted"
    assert record["resolution"]["content"] == "partial proposal\n"
    assert "rationale" not in record["resolution"]
    assert "confidence" not in record["resolution"]
    assert repo.git.conflicts()
    assert "No explanation was returned" in load_report(audit.directory)
