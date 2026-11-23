# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Audit acceptance contracts across publication, scopes and evidence policy."""

import json
import sys
from unittest.mock import patch

import pytest

from git_ez.audit import Audit
from git_ez.audit_capture import observe
from git_ez.audit_reader import load_audit
from git_ez.audit_report import render_markdown
from git_ez.audit_storage import AuditStore
from git_ez.cli import main
from git_ez.config import Config
from git_ez.engine import Engine
from git_ez.errors import EzError
from git_ez.options import RebaseOptions
from git_ez.sequence_editor import read_regular as read_sequence
from tests.test_engine import repo as repo

pytestmark = pytest.mark.integration


def success(repo, operation="merge", **options):
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", operation, **options)
    assert audit.view["status"] == "succeeded", audit.view.get("error")
    assert audit.published
    return audit


def replace_manifest(audit, payload):
    (audit.directory / "run.json").write_text(json.dumps(payload))


def test_reader_requires_conflict_input_even_if_missing_from_registry(repo):
    audit = success(repo)
    payload = json.loads((audit.directory / "run.json").read_bytes())
    artifact = payload["conflicts"][0]["artifacts"]["base"]
    del payload["artifacts"][artifact]
    payload["requirements"] = [r for r in payload["requirements"] if r.get("artifact") != artifact]
    (audit.directory / artifact).unlink()
    replace_manifest(audit, payload)
    assert main(["show", str(audit.directory)]) != 0


def test_reader_rejects_contradictory_validation_result(repo):
    audit = success(repo)
    payload = json.loads((audit.directory / "run.json").read_bytes())
    payload["checks"]["validation"] = {"command": ["false"], "status": "passed", "returncode": 7}
    replace_manifest(audit, payload)
    assert main(["show", str(audit.directory)]) != 0


@pytest.mark.parametrize("outcome", ["pending", "failed", "interrupted"])
@pytest.mark.parametrize("damage", ["returncode", "observation", "missing_step"])
def test_completed_integration_requires_native_proof_for_every_outcome(repo, outcome, damage):
    audit = success(repo)
    payload = json.loads((audit.directory / "run.json").read_bytes())
    payload["outcome"] = outcome
    if outcome == "pending":
        payload["lifecycle"] = "active"
    if damage == "returncode":
        payload["git"]["steps"][-1]["returncode"] = 7
    elif damage == "missing_step":
        payload["git"]["steps"] = []
    else:
        step = payload["git"]["steps"][-1]
        next(item for item in payload["git"]["observations"] if item["id"] == step["after"])[
            "operation_active"
        ] = True
    replace_manifest(audit, payload)
    assert main(["show", str(audit.directory)]) == 2


def test_completed_integration_stays_verifiable_after_validation_failure(repo):
    audit = Engine(repo.root, repo.config, repo.resolver).run(
        "main", "merge", validation=(sys.executable, "-c", "raise SystemExit(7)")
    )
    assert audit.view["status"] == "failed"
    assert audit.view["integration"] == "completed"
    assert audit.view["validation"]["returncode"] == 7
    assert load_audit(audit.directory).exit_code == 0


@pytest.mark.parametrize("returncode", [0, 7])
def test_validation_capture_failure_preserves_primary_domain(repo, monkeypatch, returncode):
    write = AuditStore.write

    def fail_stdout(store, artifact, content, **kwargs):
        if artifact == "validation.stdout":
            raise OSError("Synthetic output capture failure")
        return write(store, artifact, content, **kwargs)

    monkeypatch.setattr(AuditStore, "write", fail_stdout)
    audit = Engine(repo.root, repo.config, repo.resolver).run(
        "main", "merge", validation=(sys.executable, "-c", f"raise SystemExit({returncode})")
    )
    assert audit.view["integration"] == "completed"
    assert audit.view["validation"]["returncode"] == returncode
    assert audit.view["failure"]["domain"] == ("audit" if returncode == 0 else "execution")
    assert audit.view["status"] == "failed"


@pytest.mark.parametrize("phase", ["conflict", "review"])
def test_first_metadata_capture_failure_retains_audit_domain(repo, monkeypatch, phase):
    save = Audit.save
    accepted_checkpoints = 0
    failed = False

    def fail_metadata(audit):
        nonlocal accepted_checkpoints, failed
        data = audit.view
        if phase == "review" and data.get("reviews", [{}])[-1].get("decision") == "accept":
            accepted_checkpoints += 1
            should_fail = accepted_checkpoints == 2
        else:
            conflicts = data.get("conflicts", [])
            should_fail = phase == "conflict" and conflicts and "resolution" in conflicts[-1]
        if should_fail and not failed:
            failed = True
            raise OSError("Synthetic metadata capture failure")
        return save(audit)

    monkeypatch.setattr(Audit, "save", fail_metadata)
    monkeypatch.setattr("builtins.input", lambda _: "a")
    audit = Engine(repo.root, repo.config, repo.resolver).run(
        "main", "merge", interactive=phase == "review"
    )
    assert failed
    assert audit.view["status"] == "failed"
    assert audit.view["failure"]["domain"] == "audit"
    assert not audit.view["integrated"]
    assert repo.git.operation_active()


@pytest.mark.parametrize("interactive", [False, True])
def test_applied_proposal_requires_its_canonical_decision(repo, monkeypatch, interactive):
    monkeypatch.setattr("builtins.input", lambda _: "a")
    audit = success(repo, interactive=interactive)
    payload = json.loads((audit.directory / "run.json").read_bytes())
    for record in [*payload["proposals"], *payload["conflicts"]]:
        record.pop("resolution", None)
    replace_manifest(audit, payload)
    assert main(["show", str(audit.directory)]) == 2


def test_interactive_application_requires_recorded_acceptance(repo, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "a")
    audit = success(repo, interactive=True)
    payload = json.loads((audit.directory / "run.json").read_bytes())
    del payload["proposals"][0]["decision"]
    replace_manifest(audit, payload)
    assert main(["show", str(audit.directory)]) == 2


def test_reader_requires_actual_request_and_pinned_inputs(repo):
    audit = success(repo)
    payload = json.loads((audit.directory / "run.json").read_bytes())
    payload["inputs"] = {}
    payload["request"] = {"operation": "merge"}
    replace_manifest(audit, payload)
    assert main(["show", str(audit.directory)]) != 0


def test_malformed_issue_record_is_rejected_with_a_controlled_reader_error(repo):
    audit = success(repo)
    payload = json.loads((audit.directory / "run.json").read_bytes())
    payload["issues"] = ["malformed issue"]
    replace_manifest(audit, payload)
    assert main(["show", str(audit.directory)]) == 2


def test_terminal_publication_failure_preserves_interruption(repo):
    class Interrupted:
        def run(self, *args):
            raise KeyboardInterrupt()

    snapshot = AuditStore.snapshot

    def fail_terminal(store, payload):
        if payload["lifecycle"] == "finished":
            raise OSError("review terminal fault")
        return snapshot(store, payload)

    with patch.object(AuditStore, "snapshot", fail_terminal):
        audit = Engine(repo.root, repo.config, Interrupted()).run("main", "merge")
    assert audit.view["failure"]["type"] == "KeyboardInterrupt"
    assert audit.view["status"] == "interrupted"


def test_finalized_owner_cannot_change_committed_check_result(repo):
    audit = success(repo)
    saved = (audit.directory / "run.json").read_bytes()
    with pytest.raises(ValueError):
        audit.checked(EzError("late failure"))
    assert (audit.directory / "run.json").read_bytes() == saved
    assert audit.view["final_checks"] == "passed"


def test_json_conflict_request_filters_escaped_known_credentials(repo):
    token = 'review-quote"token\\suffix'
    repo.file.write_text("source " + token + "\n")
    repo.git.run("commit", "-am", "source fixture", "--amend")
    repo.config = Config(model="test", api_key=token)
    audit = success(repo)
    artifact = audit.view["conflicts"][0]["artifacts"]["request"]
    request = json.loads((audit.directory / artifact).read_bytes())
    assert not any(token in value for value in request.values() if isinstance(value, str))


def test_native_sequence_filters_known_credentials(repo):
    token = "review-configured-token"
    repo.git.run("commit", "--amend", "-m", "source " + token)
    repo.config = Config(model="test", api_key=token)
    audit = success(repo, "rebase", rebase_options=RebaseOptions(autosquash=True))
    artifact = audit.view["rebase_sequence"]["artifact"]
    assert token.encode() not in (audit.directory / artifact).read_bytes()


def test_interactive_failure_does_not_persist_arbitrary_exception_message(repo):
    message = "synthetic unconfigured sensitive exception body"

    class Broken:
        def run(self, *args):
            raise ValueError(message)

    audit = Engine(repo.root, repo.config, Broken()).run("main", "merge", interactive=True)
    assert audit.view["error"] == "Unexpected ValueError"
    assert message not in (audit.directory / "run.json").read_text()


def test_range_capture_failure_does_not_prevent_independent_commit_log(repo):
    write = AuditStore.write
    attempted = []

    def fail_range(store, relative, content, **kwargs):
        attempted.append(relative)
        if relative == "range-diff.txt":
            raise OSError("review range fault")
        return write(store, relative, content, **kwargs)

    with patch.object(AuditStore, "write", fail_range):
        audit = Engine(repo.root, repo.config, repo.resolver).run(
            "main", "rebase", rebase_options=RebaseOptions(autosquash=True)
        )
    assert audit.view["integrated"]
    assert "rebase-commits.log" in attempted


def test_unavailable_optional_export_still_produces_a_reader_warning(repo):
    audit = success(repo)
    (audit.directory / "config.json").unlink()
    view = load_audit(audit.directory)
    assert view.exit_code == 0
    assert any("config.json" in message for message in view.diagnostics)


def test_unknown_completion_is_explicit_in_the_rich_report(repo):
    count = 0

    def lose_post_observation(git):
        nonlocal count
        count += 1
        observation = observe(git)
        if count == 2:
            observation["head"] = None
            observation["errors"].append({"field": "head", "type": "ReviewObservationError"})
        return observation

    with patch("git_ez.audit.observe", lose_post_observation):
        audit = Engine(repo.root, repo.config, repo.resolver).run("main", "merge")
    assert audit.view["integration"] == "unknown"
    report = render_markdown(audit.view)
    known_stopped = dict(audit.view, integration="not_completed")
    assert render_markdown(known_stopped) != report


def test_rerere_application_has_precontinuation_staged_evidence(repo):
    repo.git.run("config", "rerere.enabled", "true")
    success(repo, rerere_autoupdate=True)
    repo.git.run("reset", "--hard", repo.source_oid)
    repo.resolver.calls.clear()
    audit = success(repo, rerere_autoupdate=True)
    assert not repo.resolver.calls
    assert audit.view["native_resolutions"]
    assert any(entry.get("role") == "staged_diff" for entry in audit.evidence.values())


def test_git_failure_remains_primary_when_output_capture_also_fails(repo):
    repo.git.run("checkout", "-B", "feature", repo.base_oid)
    (repo.root / "independent.txt").write_text("independent\n")
    repo.git.run("add", ".")
    repo.git.run("commit", "-m", "independent fixture")
    hook = repo.root / ".git/hooks/pre-merge-commit"
    hook.write_text("#!/bin/sh\nexit 7\n")
    hook.chmod(0o700)
    write = AuditStore.write

    def fail_output(store, relative, content, **kwargs):
        if relative.startswith("git-steps/") and relative.endswith(".stderr"):
            raise OSError("review output fault")
        return write(store, relative, content, **kwargs)

    with patch.object(AuditStore, "write", fail_output):
        audit = Engine(repo.root, repo.config, repo.resolver).run("main", "merge")
    payload = json.loads((audit.directory / "run.json").read_bytes())
    assert payload["git"]["steps"][0]["returncode"] != 0
    assert not repo.resolver.calls
    assert audit.view["failure"]["domain"] == "execution"


@pytest.mark.parametrize("token", ["core_fact", "HEAD", "files", "stage", "integration", "applied"])
def test_serialization_rechecks_invariants_after_redaction(repo, token):
    # A weak configured token must not corrupt policy facts through redaction.
    repo.config = Config(model="test", api_key=token)
    audit = success(repo)
    assert load_audit(audit.directory).exit_code == 0


def test_truncated_result_diff_cannot_satisfy_success(repo, monkeypatch):
    write = Audit.write

    def bounded(audit, artifact, content, **kwargs):
        if artifact == "result.diff":
            kwargs["retained_limit"] = 8
        return write(audit, artifact, content, **kwargs)

    monkeypatch.setattr(Audit, "write", bounded)
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", "merge")
    assert audit.evidence["result.diff"]["truncated"]
    assert audit.view["status"] == "failed"
    assert audit.view["integrated"]
    assert load_audit(audit.directory).exit_code == 1


def test_sequence_source_rejects_symlinks_in_parent_components(repo):
    real = repo.root / "actual"
    real.mkdir()
    (real / "todo").write_bytes(b"noop\n")
    alias = repo.root / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        read_sequence(alias / "todo", 32)


def test_file_source_byte_count_records_the_known_original_size(repo):
    path = repo.root / "large.txt"
    path.write_bytes(b"x" * 200)
    audit = Engine(repo.root, repo.config, repo.resolver)._prepare_run(
        "feature", repo.source_oid, "main", repo.parent_oid, "refs/heads/main", "merge"
    )
    audit.initialize()
    audit.capture_worktree(repo.git, audit.directory / "capture", 32)
    inventory = json.loads((audit.directory / "capture/files.json").read_bytes())
    record = next(record for record in inventory["files"] if record["path"] == "large.txt")
    artifact = "capture/" + record["artifact"]
    assert record["size"] == 200
    assert audit.evidence[artifact]["source_bytes"] == 200


def test_uncertain_terminal_commit_is_not_claimed_absent_or_retried(repo):
    snapshot = AuditStore.snapshot
    terminal_attempts = []

    def lose_acknowledgement(store, payload):
        result = snapshot(store, payload)
        if payload["lifecycle"] == "finished":
            terminal_attempts.append(payload["revision"])
            raise OSError("Synthetic loss of publication acknowledgement")
        return result

    with patch.object(AuditStore, "snapshot", lose_acknowledgement):
        audit = Engine(repo.root, repo.config, repo.resolver).run("main", "merge")
    result = audit.terminal_result
    assert result.publication == "unknown"
    assert result.attempted_revision == terminal_attempts[0]
    assert result.published_revision < result.attempted_revision
    assert not audit.published
    assert audit.view["status"] == "failed"
    assert len(terminal_attempts) == 1
    assert load_audit(audit.directory).data["status"] == "succeeded"


def test_reader_checks_inventory_children_independently_of_declared_registry(repo):
    audit = success(repo)
    payload = json.loads((audit.directory / "run.json").read_bytes())
    artifact = next(
        path
        for path in payload["artifacts"]
        if path.startswith("proposals/") and path.endswith(".bin")
    )
    del payload["artifacts"][artifact]
    payload["requirements"] = [r for r in payload["requirements"] if r.get("artifact") != artifact]
    (audit.directory / artifact).unlink()
    replace_manifest(audit, payload)
    assert load_audit(audit.directory).exit_code == 1


def test_rerere_does_not_continue_when_its_staged_capture_fails(repo):
    repo.git.run("config", "rerere.enabled", "true")
    success(repo, rerere_autoupdate=True)
    repo.git.run("reset", "--hard", repo.source_oid)
    write = AuditStore.write

    def lose_staged_proof(store, relative, content, **kwargs):
        if relative.startswith("native-resolutions/"):
            raise OSError("Synthetic loss of rerere staging evidence")
        return write(store, relative, content, **kwargs)

    with patch.object(AuditStore, "write", lose_staged_proof):
        audit = Engine(repo.root, repo.config, repo.resolver).run(
            "main", "merge", rerere_autoupdate=True
        )
    assert audit.view["status"] == "failed"
    assert not audit.view["integrated"]
    assert repo.git.operation_active()
    assert repo.git.text("rev-parse", "HEAD") == repo.source_oid


def test_reader_labels_raw_orphans_without_reading_them(repo):
    audit = success(repo)
    source = repo.root / "unrelated"
    source.write_text("private content that must not be read")
    (audit.directory / "orphan\nunsafe").symlink_to(source)
    original = (audit.directory / "run.json").read_bytes()
    view = load_audit(audit.directory)
    assert view.exit_code == 0
    assert any("Unindexed raw artifact" in warning for warning in view.diagnostics)
    assert all("\n" not in warning for warning in view.diagnostics)
    assert (audit.directory / "run.json").read_bytes() == original


def test_native_sequence_secret_transport_does_not_use_arguments_or_environment(repo):
    token = "synthetic-credential-for-sequence-test"
    repo.config = Config(model="test", api_key=token)
    # Capture the subprocess boundary used by both engine and helper, without
    # printing environment contents or introducing an external model.
    import subprocess

    execute = subprocess.run
    descriptors = []

    def inspect_call(*args, **kwargs):
        if kwargs.get("pass_fds"):
            descriptors.extend(kwargs["pass_fds"])
            assert not any(token in str(argument) for argument in args[0])
            assert token not in kwargs.get("env", {}).values()
        return execute(*args, **kwargs)

    with patch("git_ez.git.subprocess.run", inspect_call):
        success(repo, "rebase", rebase_options=RebaseOptions(autosquash=True))
    assert descriptors


def test_native_sequence_capture_failure_is_audit_failure_before_replay(repo):
    repo.config = Config(model="test", max_file_bytes=32)
    audit = Engine(repo.root, repo.config, repo.resolver).run(
        "main", "rebase", rebase_options=RebaseOptions(autosquash=True)
    )
    assert audit.view["status"] == "failed"
    assert audit.view["failure"]["domain"] == "audit"
    assert audit.view["failure"]["phase"] == "sequence_capture"
    assert repo.git.text("rev-parse", "HEAD") == repo.source_oid
    assert not repo.resolver.calls


def test_operation_observation_failure_is_not_reported_as_inactive(repo):
    from pathlib import Path

    stat = Path.lstat

    def denied(path, *args, **kwargs):
        if path == repo.root / ".git/rebase-merge":
            raise PermissionError("Synthetic marker observation failure")
        return stat(path, *args, **kwargs)

    with patch.object(Path, "lstat", denied):
        observation = observe(repo.git)
    assert observation["operation_active"] is None
    assert any(error["field"] == "operation" for error in observation["errors"])


def test_unsupported_binary_conflict_retains_all_available_inputs_without_resolving(repo):
    repo.file.write_bytes(b"source\x00fixture")
    repo.git.run("commit", "--amend", "-am", "binary fixture")
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", "merge")
    assert audit.view["status"] == "failed"
    assert not repo.resolver.calls
    record = audit.view["conflicts"][0]
    assert record["input_status"] == "unsupported"
    assert {"base", "ours", "theirs"} <= set(record["artifacts"])
    for label in ("base", "ours", "theirs"):
        assert audit.has_artifact(record["artifacts"][label])


def test_closed_session_requires_its_worktree_evidence_before_staging(repo):
    resolver = repo.resolver.run

    def omit_session_capture(git, audit, round_number, paths, check):
        decision = resolver(git, audit, round_number, paths, check)
        audit.add_record(
            "sessions",
            {
                "status": "failed",
                "round": round_number,
                "artifacts": "sessions/missing",
                "runtime": "deterministic fixture",
                "tool_calls": 0,
            },
        )
        return decision

    with patch.object(repo.resolver, "run", omit_session_capture):
        audit = Engine(repo.root, repo.config, repo.resolver).run("main", "merge")
    assert audit.view["status"] == "failed"
    assert repo.git.conflicts()
    assert repo.git.text("rev-parse", "HEAD") == repo.source_oid
    payload = json.loads((audit.directory / "run.json").read_bytes())
    assert not any(step["purpose"] == "stage" for step in payload["git"]["steps"])
