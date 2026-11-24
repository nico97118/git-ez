# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Proposal review, revision, and acceptance during integration."""

import pytest

from git_ez.cli import main
from git_ez.engine import Engine
from git_ez.errors import EzError
from tests.helpers import integrate

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("operation", ["merge", "rebase"])
@pytest.mark.parametrize("choice", ["accept", "stop", "eof", "interrupt", "revise"])
def test_interactive_review_preserves_or_applies_proposals(repo, mocker, operation, choice):
    answers = iter(
        ["r", "add a test", "r", "keep both intentions", "a"]
        if choice == "revise"
        else ["a" if choice == "accept" else "s"]
    )
    observed = []
    original = repo.resolver.run

    def resolve(git, audit, number, paths, check):
        observed.append((repo.file.read_text(), audit.view["reviews"][-1]["feedback"]))
        return original(git, audit, number, paths, check)

    repo.resolver.run = resolve

    def read(prompt):
        assert repo.git.run("ls-files", "-u").stdout
        if choice == "eof":
            raise EOFError
        if choice == "interrupt":
            raise KeyboardInterrupt
        return next(answers)

    mocker.patch("builtins.input", side_effect=read)
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", operation, interactive=True)
    success = choice in ("accept", "revise")
    assert (audit.view["status"] == "succeeded") == success, audit.view
    assert bool(repo.git.run("ls-files", "-u").stdout) != success
    assert repo.file.read_text() == ("combined\n" if success else observed[0][0])
    reviews = audit.view["reviews"]
    assert len(reviews) == (3 if choice == "revise" else 1)
    for proposal in reviews:
        assert (audit.directory / proposal["artifacts"] / "files.json").is_file()
    if choice == "revise":
        assert observed[1:] == [
            (observed[0][0], "add a test"),
            (observed[0][0], "keep both intentions"),
        ]


def test_interactive_review_rechecks_state(repo, mocker):
    def read(prompt):
        repo.git.run("update-ref", "refs/heads/main", repo.base_oid)
        return "a"

    mocker.patch("builtins.input", side_effect=read)
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", interactive=True)
    assert audit.view["status"] == "failed"
    assert repo.git.run("ls-files", "-u").stdout


@pytest.mark.parametrize("mutation", ["file", "index"])
def test_interactive_review_rejects_concurrent_edits(repo, mocker, mutation):
    def read(prompt):
        if mutation == "file":
            repo.file.write_text("changed during review\n")
        else:
            repo.git.run("add", "file.txt")
        return "a"

    mocker.patch("builtins.input", side_effect=read)
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", interactive=True)
    assert audit.view["status"] == "failed"
    assert "changed during review" in audit.view["error"]
    assert repo.git.rebase_active()


@pytest.mark.parametrize("operation", ["merge", "rebase"])
def test_interactive_without_conflict_never_prompts(repo, mocker, operation):
    prompt = mocker.patch("builtins.input", side_effect=AssertionError("Unexpected prompt"))
    audit = Engine(repo.root, repo.config, repo.resolver).run("HEAD", operation, interactive=True)
    assert audit.view["status"] == "succeeded"
    assert not repo.resolver.calls
    prompt.assert_not_called()


def test_interactive_acceptance_does_not_bypass_confidence(repo, mocker):
    repo.resolver.confidence = 0.01
    mocker.patch("builtins.input", return_value="a")
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", interactive=True)
    assert audit.view["status"] == "failed"
    assert repo.git.run("ls-files", "-u").stdout
    assert audit.view["reviews"][0]["decision"] == "accept"


def test_interactive_multiple_rebase_stops(repo, mocker):
    repo.file.write_text("source second\n")
    repo.git.run("commit", "-am", "second change")
    mocker.patch("builtins.input", side_effect=["r", "revise first", "a", "a"])
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", interactive=True)
    assert audit.view["status"] == "succeeded", audit.view
    assert [r["round"] for r in audit.view["reviews"]] == [1, 1, 2]


@pytest.mark.parametrize(
    ("operation", "previous_stop"), [("merge", False), ("rebase", False), ("rebase", True)]
)
@pytest.mark.parametrize("ending", ["stop", "eof", "interrupt", "resolver_error"])
def test_interactive_failed_revision_links_latest_session(
    repo, mocker, operation, ending, previous_stop
):
    if previous_stop:
        repo.file.write_text("source second\n")
        repo.git.run("commit", "-am", "second change")
    original = repo.resolver.run
    indices = []
    heads = []
    failing_round = 2 if previous_stop else 1

    def resolve(git, audit, number, paths, check):
        review = audit.view["reviews"][-1]
        directory = audit.directory / "sessions" / f"{number:04d}"
        audit.create_directory(directory)
        audit.add_record(
            "sessions",
            {
                "round": number,
                "artifacts": f"sessions/{number:04d}",
                "tool_calls": 0,
                "runtime": "deterministic test resolver",
                "status": "failed",
                "rationale": f"attempt {number}",
                "confidence": 0.95,
            },
        )
        audit.write_json(f"sessions/{number:04d}/messages.json", [f"attempt {number}"])
        if review["round"] == failing_round:
            indices.append(git.run("ls-files", "--stage", "-z").stdout)
            heads.append(git.text("rev-parse", "HEAD"))
        result = original(git, audit, number, paths, check)
        if review["feedback"] and ending == "resolver_error":
            raise EzError("Revision resolver failed")
        return result

    repo.resolver.run = resolve
    answers = iter((["a"] if previous_stop else []) + ["r", "revise this proposal", "s"])

    def read(prompt):
        if len(indices) == 2:
            if ending == "eof":
                raise EOFError
            if ending == "interrupt":
                raise KeyboardInterrupt
        return next(answers)

    mocker.patch("builtins.input", side_effect=read)
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", operation, interactive=True)
    assert audit.view["status"] == ("interrupted" if ending == "interrupt" else "failed")
    latest = 3 if previous_stop else 2
    record = audit.view["conflicts"][-1]
    assert record["artifacts"]["session"] == f"sessions/{latest:04d}/messages.json"
    assert record["resolution"]["rationale"] == f"attempt {latest}"
    assert record["status"] == ("interrupted" if ending == "interrupt" else "failed")
    if previous_stop:
        assert audit.view["conflicts"][0]["artifacts"]["session"] == "sessions/0001/messages.json"
    assert len(audit.view["reviews"]) == latest
    for review in audit.view["reviews"]:
        assert (audit.directory / review["artifacts"] / "files.json").is_file()
        assert (audit.directory / review["artifacts"] / "worktree.diff").is_file()
    assert indices[0] == indices[1] == repo.git.run("ls-files", "--stage", "-z").stdout
    assert heads[0] == heads[1] == repo.git.text("rev-parse", "HEAD")
    assert repo.git.run("ls-files", "-u").stdout
    if operation == "rebase":
        assert repo.git.rebase_active()
    else:
        assert repo.git.text("rev-parse", "MERGE_HEAD") == repo.parent_oid
    assert "<<<<<<< HEAD" in repo.file.read_text()


@pytest.mark.parametrize("operation", ["merge", "rebase"])
def test_interactive_rerere_autoupdate_never_prompts(repo, mocker, operation):
    repo.git.run("config", "rerere.enabled", "true")
    first = integrate(repo, operation, rerere_autoupdate=True)
    assert first.view["status"] == "succeeded"
    repo.git.run("reset", "--hard", repo.source_oid)
    repo.resolver.calls.clear()
    mocker.patch("builtins.input", side_effect=AssertionError("Unexpected prompt"))
    second = Engine(repo.root, repo.config, repo.resolver).run(
        "main", operation, rerere_autoupdate=True, interactive=True
    )
    assert second.view["status"] == "succeeded", second.view
    assert not repo.resolver.calls


def test_interactive_global_merge_approval_covers_all_files(repo, mocker, capsys):
    (repo.root / "second.txt").write_text("source addition\n")
    repo.git.run("add", ".")
    repo.git.run("commit", "-m", "source addition")
    repo.git.run("checkout", "main")
    (repo.root / "second.txt").write_text("parent addition\n")
    repo.git.run("add", ".")
    repo.git.run("commit", "-m", "parent addition")
    repo.git.run("checkout", "feature")
    prompt = mocker.patch("builtins.input", return_value="a")
    audit = integrate(repo, "merge", interactive=True)
    assert audit.view["status"] == "succeeded", audit.view
    assert len(audit.view["reviews"]) == 1
    assert len(audit.view["reviews"][0]["conflict_ids"]) == 2
    prompt.assert_called_once()
    output = capsys.readouterr().out
    assert "file.txt" in output and "second.txt" in output


@pytest.mark.parametrize("interactive", [False, True])
def test_cli_interruption_exit_status_preserves_noninteractive_policy(repo, mocker, interactive):
    repo.git.run("config", "ez.model", "test")
    engine = Engine(repo.root, repo.config, repo.resolver)
    mocker.patch("git_ez.cli.Engine", return_value=engine)
    mocker.patch.object(repo.resolver, "run", side_effect=KeyboardInterrupt)
    arguments = ["rebase", "main", "--repo", str(repo.root)]
    if interactive:
        arguments.append("--interactive")
    assert main(arguments) == (130 if interactive else 1)
    assert repo.git.conflicts()
    assert repo.git.rebase_active()
