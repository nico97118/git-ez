# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Audit truth survives publication failure, stale presentation and missing evidence."""

import json
import os
import select
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from git_ez.audit import Audit
from git_ez.audit_reader import load_audit
from git_ez.audit_report import render_markdown
from git_ez.audit_safety import redact_chunks
from git_ez.audit_state import now, validate_snapshot
from git_ez.audit_storage import AuditStore, publish, read_regular
from git_ez.cli import main
from git_ez.engine import Engine
from git_ez.errors import AuditError, EzError
from tests.test_engine import repo as repo


@pytest.fixture
def audit(tmp_path):
    instance = Audit(
        tmp_path / "runs" / "audit",
        {
            "created_at": now(),
            "operation": "merge",
            "target": "main",
            "branch": "feature",
            "before_oid": "1" * 40,
            "target_oid": "2" * 40,
            "target_ref": "refs/heads/main",
            "worktree": str(tmp_path),
            "provider": "test",
            "model": "test",
            "conflicts": [],
            "configuration": {"api_key": "<redacted>"},
            "prompt": "Resolve conflicts",
        },
    )
    instance.initialize()
    return instance


@pytest.mark.unit
def test_constructor_does_not_create_or_claim_directory(tmp_path):
    directory = tmp_path / "run"
    instance = Audit(directory, {"worktree": str(tmp_path)})
    assert instance.view["status"] == "running"
    assert not directory.exists()


@pytest.mark.unit
def test_exported_state_cannot_mutate_execution_or_evidence(audit):
    view = audit.view
    view["status"] = "succeeded"
    view["configuration"]["api_key"] = "leaked"
    assert audit.view["status"] == "running"
    assert audit.view["configuration"]["api_key"] == "<redacted>"
    audit.write("proof.txt", b"original")
    proof = audit.evidence
    proof["proof.txt"]["sha256"] = "wrong"
    assert audit.has_artifact("proof.txt")


@pytest.mark.unit
@pytest.mark.parametrize(
    "error, expected", [(EzError("Changed HEAD"), "failed"), (OSError(), "unknown")]
)
def test_final_checks_distinguish_rejected_invariant_from_unavailable_observation(
    audit, error, expected
):
    audit.checked(error)
    assert audit.view["final_checks"] == expected
    assert audit.view["final_check_facts"]["status"] == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "action",
    [
        "checked",
        "completed",
        "result",
        "expect",
        "unavailable",
        "protect",
        "write",
        "append_event",
        "create_directory",
        "register_external",
        "capture_error",
    ],
)
def test_terminal_result_rejects_new_facts_and_remains_frozen(audit, action):
    audit.set_status("failed", EzError("Stopped fixture"))
    audit.finalize()
    result = audit.terminal_result
    assert result is not None
    before = audit.view
    operations = {
        "checked": lambda: audit.checked(EzError("Late failure")),
        "completed": lambda: audit.completed("new result"),
        "result": lambda: audit.result("new result"),
        "expect": lambda: audit.expect("late", "proof"),
        "unavailable": lambda: audit.unavailable("late", "late"),
        "protect": lambda: audit.protect("late"),
        "write": lambda: audit.write("late", b"late"),
        "append_event": lambda: audit.append_event("late", {"event": "late"}),
        "create_directory": lambda: audit.create_directory(audit.directory / "late"),
        "register_external": lambda: audit.register_external("late"),
        "capture_error": lambda: audit.capture_error("late", OSError(), None),
    }
    with pytest.raises(ValueError):
        operations[action]()
    assert audit.view == before
    assert audit.terminal_result is result
    result.view["status"] = "succeeded"
    assert audit.view == before


@pytest.mark.unit
@pytest.mark.parametrize(
    "secrets, content, expected",
    [
        ((b"",), b"ordinary", b"ordinary"),
        ((b"redacted",), b"<redacted> redacted", b"<redacted> <redacted>"),
    ],
)
def test_stream_filter_handles_empty_secrets_and_existing_placeholders(secrets, content, expected):
    assert b"".join(redact_chunks((bytes([value]) for value in content), secrets)) == expected


@pytest.mark.unit
def test_invalid_scope_update_cannot_poison_finalization(audit):
    record = audit.add_record(
        "sessions",
        {
            "round": 1,
            "status": "running",
            "artifacts": "sessions/1",
        },
    )
    with pytest.raises(ValueError):
        record.update(artifacts=[])
    assert record["artifacts"] == "sessions/1"
    audit.set_status("failed", EzError("Stopped before invocation"))
    audit.finalize()
    assert audit.published


@pytest.mark.unit
@pytest.mark.parametrize("group, status", [("reviews", "proposed"), ("sessions", "resolved")])
def test_scope_cannot_claim_a_decision_without_its_required_facts(audit, group, status):
    record = audit.add_record(group, {"round": 1, "status": "running", "artifacts": f"{group}/1"})
    with pytest.raises(ValueError, match="canonical decision"):
        record.update(status=status)
    assert record["status"] == "running"
    audit.set_status("failed", EzError("Stopped before a decision"))
    audit.finalize()
    assert audit.published


@pytest.mark.unit
def test_tool_feedback_receipts_are_scoped_to_each_invocation(audit):
    from git_ez.agent_runtime import SessionLog

    log = SessionLog(audit.directory / "sessions/1", audit)
    call = {"id": "reused", "name": "unknown", "args": {}}
    for _ in range(2):
        log.begin_tool(call["id"])
        result = log.feedback(call, "Unknown tool")
        log.record_feedback(call, result)  # Already acknowledged in this invocation.
    events = [
        json.loads(line)
        for line in (audit.directory / "sessions/1/events.jsonl").read_text().splitlines()
    ]
    assert [event["event"] for event in events] == ["tool_requested", "tool_result"] * 2
    assert log.tools == 2


@pytest.mark.unit
def test_failure_precedence_and_terminal_publication(audit):
    audit.set_status("failed", EzError("initial failure"), phase="integration")
    audit.attempt("secondary", lambda: (_ for _ in ()).throw(OSError("private detail")))
    audit.set_status("failed", EzError("cleanup failure"))
    audit.finalize()
    loaded = load_audit(audit.directory)
    assert loaded.data["error"] == "initial failure"
    assert "private detail" not in (audit.directory / "run.json").read_text()
    assert audit.published
    original = (audit.directory / "run.json").read_bytes()
    audit.finalize()
    assert (audit.directory / "run.json").read_bytes() == original


@pytest.mark.unit
def test_known_credentials_are_redacted_before_json_encoding(audit):
    secret = 'a"configured-token'
    audit.protect(secret)
    audit.annotate(notes={"nested": [secret]})
    audit.write_json("export.json", {"text": secret})
    audit.set_status("failed", EzError("failed with " + secret))
    audit.finalize()
    for path in audit.directory.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()
            assert json.dumps(secret)[1:-1].encode() not in path.read_bytes()
    assert load_audit(audit.directory).data["notes"]["nested"] == ["<redacted>"]


@pytest.mark.unit
def test_json_redaction_preserves_keys_and_schema_values(audit):
    audit.protect("files", "file")
    audit.write_json(
        "inventory.json", {"files": [{"state": "file", "path": "files/private"}]}, schema=True
    )
    data = json.loads((audit.directory / "inventory.json").read_bytes())
    assert data == {"files": [{"state": "file", "path": "<redacted>/private"}]}
    assert audit.evidence["inventory.json"]["redacted"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "role, accepted",
    [
        (role, False)
        for role in (
            "proof",
            "conflict_input",
            "conflict_proposal",
            "result_diff",
            "range_diff",
            "staged_diff",
            "git_step_stdout",
            "git_step_stderr",
            "rebase_sequence",
        )
    ]
    + [(role, True) for role in ("file_content", "commit_log", "rebase_selection")],
)
def test_capture_prefix_policy_controls_live_and_offline_evidence(audit, role, accepted):
    if accepted:
        audit.write("capture.bin", b"long contents", role=role, retained_limit=4)
    else:
        with pytest.raises(AuditError):
            audit.write("capture.bin", b"long contents", role=role, retained_limit=4)
    assert audit.evidence["capture.bin"]["truncated"]
    assert audit.evidence_ready is accepted
    audit.set_status("failed", EzError("Stopped fixture"))
    audit.finalize()
    assert load_audit(audit.directory).exit_code == (0 if accepted else 1)


@pytest.mark.unit
def test_transformed_manifest_is_validated_before_replacing_checkpoint(audit):
    previous = (audit.directory / "run.json").read_bytes()
    redact = audit._redact_schema

    def invalidate(data):
        result = redact(data)
        if isinstance(result, dict) and "schema_version" in result:
            result["outcome"] = "invalid"
        return result

    with patch.object(audit, "_redact_schema", side_effect=invalidate), pytest.raises(EzError):
        audit.save()
    assert (audit.directory / "run.json").read_bytes() == previous


@pytest.mark.unit
@pytest.mark.parametrize("split", range(1, 12))
def test_redaction_preserves_credentials_across_chunk_boundaries(split):
    content = b"before configured-token after configured-token"
    chunks = (content[index : index + split] for index in range(0, len(content), split))
    assert b"".join(redact_chunks(chunks, (b"configured-token",))) == (
        b"before <redacted> after <redacted>"
    )


@pytest.mark.unit
def test_sdk_files_are_sealed_privately_and_redacted_before_registration(audit):
    audit.protect("configured-token")
    directory = audit.directory / "session/agent-files"
    audit.create_directory(directory)
    path = directory / "history.txt"
    path.write_bytes(b"x" * 65530 + b"configured-token" + b" history")
    audit.seal_directory(directory)
    assert path.read_bytes() == b"x" * 65530 + b"<redacted> history"
    assert path.stat().st_mode & 0o777 == 0o600
    assert audit.evidence["session/agent-files/history.txt"]["redacted"]
    assert audit.has_artifact("session/agent-files/history.txt")


@pytest.mark.unit
def test_streamed_publication_failure_preserves_the_previous_snapshot(tmp_path):
    path = tmp_path / "snapshot"
    publish(path, b"old", replace=True)

    def chunks():
        yield b"new prefix"
        raise OSError("source failed")

    with pytest.raises(OSError):
        publish(path, chunks(), replace=True)
    assert read_regular(path) == b"old"


@pytest.mark.unit
def test_optional_export_failure_does_not_become_a_primary_execution_failure(audit):
    error = audit.attempt(
        "optional export", lambda: (_ for _ in ()).throw(OSError()), "optional.json", optional=True
    )
    assert error is not None
    assert audit.evidence_health == "degraded"
    assert audit.view["status"] == "running"
    assert "failure" not in audit.view


@pytest.mark.unit
def test_artifact_names_cannot_inject_terminal_controls_or_report_headings(audit):
    audit.write("agent-files/evil`\n# forged\x1b.bin", b"content")
    report = render_markdown(audit.view)
    assert "\n# forged" not in report
    assert "\x1b" not in report
    assert "evil\\u0060\\u000a# forged\\u001b.bin" in report


@pytest.mark.unit
def test_concurrent_callback_records_are_complete_and_keep_distinct_identities(audit):
    artifact = "session/events.jsonl"

    def append(index):
        audit.append_event(artifact, {"event": "callback", "run_id": f"sdk-{index}"})

    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(append, range(40)))
    records = [json.loads(line) for line in (audit.directory / artifact).read_bytes().splitlines()]
    assert len(records) == 40
    assert {record["run_id"] for record in records} == {audit.directory.name}
    assert {record["callback_run_id"] for record in records} == {f"sdk-{i}" for i in range(40)}
    assert [record["sequence"] for record in records] == list(range(1, 41))
    assert audit.evidence[artifact]["records"] == 40
    assert audit.has_artifact(artifact)


@pytest.mark.unit
def test_partial_event_append_keeps_only_acknowledged_prefix(audit):
    stream = "sessions/1/events.jsonl"
    audit.append_event(stream, {"event": "first"})
    prefix = audit.evidence[stream]
    original = audit._store.append

    def partial_append(path, content):
        original(path, content[:3])
        raise OSError("write interrupted")

    with patch.object(audit._store, "append", partial_append):
        assert (
            audit.attempt(
                "event", lambda: audit.append_event(stream, {"event": "lost"}), stream, append=True
            )
            is not None
        )
    assert (
        audit.attempt(
            "event", lambda: audit.append_event(stream, {"event": "later"}), stream, append=True
        )
        is not None
    )
    assert audit.evidence[stream]["retained_bytes"] == prefix["retained_bytes"]
    audit.set_status("failed", EzError("required event lost"))
    audit.finalize()
    assert load_audit(audit.directory).exit_code == 1
    assert b"later" not in (audit.directory / stream).read_bytes()


@pytest.mark.unit
@pytest.mark.parametrize("phase", ["write", "replace"])
def test_atomic_replacement_preserves_previous_snapshot(tmp_path, phase):
    path = tmp_path / "run.json"
    publish(path, b'{"revision":1}', replace=True)
    operation = "os.write" if phase == "write" else "os.replace"
    with (
        patch("git_ez.audit_storage." + operation, side_effect=OSError("storage failed")),
        pytest.raises(OSError),
    ):
        publish(path, b'{"revision":2}', replace=True)
    assert json.loads(read_regular(path)) == {"revision": 1}


@pytest.mark.unit
def test_published_capture_cannot_be_overwritten(tmp_path):
    path = tmp_path / "proof"
    publish(path, b"first")
    with pytest.raises(FileExistsError):
        publish(path, b"second")
    assert read_regular(path) == b"first"
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["symlink", "parent_symlink", "fifo"])
def test_safe_storage_does_not_follow_links_or_block_on_special_files(tmp_path, kind):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret"
    secret.write_bytes(b"private")
    source = tmp_path / "source"
    if kind == "symlink":
        source.symlink_to(secret)
    elif kind == "parent_symlink":
        link = tmp_path / "linked"
        link.symlink_to(outside, target_is_directory=True)
        source = link / "secret"
    else:
        os.mkfifo(source)
    with pytest.raises((OSError, ValueError)):
        read_regular(source)
    assert secret.read_bytes() == b"private"


@pytest.mark.unit
def test_invalid_success_is_rejected_before_it_can_be_read(audit):
    payload = json.loads((audit.directory / "run.json").read_text())
    payload.update(lifecycle="finished", outcome="succeeded")
    with pytest.raises(ValueError, match="success"):
        validate_snapshot(payload)


@pytest.mark.unit
def test_reader_rejects_health_that_contradicts_required_capture_failure(audit):
    audit.expect("missing.bin", "proof")
    audit.set_status("failed", EzError("Evidence unavailable"))
    audit.finalize()
    payload = json.loads((audit.directory / "run.json").read_bytes())
    payload["audit_health"] = "complete"
    (audit.directory / "run.json").write_text(json.dumps(payload))
    with pytest.raises(EzError, match="Cannot verify audit manifest"):
        load_audit(audit.directory)


@pytest.mark.unit
def test_truncated_conflict_proposal_cannot_satisfy_evidence_gate(audit):
    audit.write("proposal.bin", b"prefix", role="conflict_proposal")
    audit.limit_evidence("proposal.bin", "truncated", "Source exceeds capture limit")
    assert not audit.evidence_ready
    audit.set_status("failed", EzError("Proposal incomplete"))
    audit.finalize()
    assert load_audit(audit.directory).exit_code == 1


@pytest.mark.unit
def test_successful_staging_cannot_establish_native_integration_completion(audit):
    observation = {"id": "observation", "head": "result", "errors": [], "operation_active": False}
    with patch("git_ez.audit.observe", side_effect=[observation, dict(observation, id="after")]):
        index = audit.begin_step(SimpleNamespace(), ("add", "-A", "--", "."))
        assert audit.returned(SimpleNamespace(), index, 0)
    assert not audit.view["integrated"]
    assert "result_oid" not in audit.view


@contextmanager
def paused_writer(script, *arguments):
    source = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join([str(source), str(source / "src")])
    process = subprocess.Popen(
        [sys.executable, "-c", script, *map(str, arguments)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    try:
        assert select.select([process.stdout], [], [], 30)[0], "Writer did not reach barrier"
        assert process.stdout.readline() == "ready\n"
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=30)


@pytest.mark.integration
@pytest.mark.parametrize("phase", ["before", "after"])
def test_killed_writer_leaves_one_complete_snapshot(tmp_path, phase):
    path = tmp_path / "run.json"
    publish(path, b'{"revision":1}', replace=True)
    script = """
import os, sys
from pathlib import Path
from git_ez.audit_storage import publish
original = os.replace
def replace(*args, **kwargs):
    if sys.argv[2] == 'after':
        original(*args, **kwargs)
    print('ready', flush=True)
    input()
    if sys.argv[2] == 'before':
        original(*args, **kwargs)
os.replace = replace
publish(Path(sys.argv[1]), b'{"revision":2}', replace=True)
"""
    with paused_writer(script, path, phase):
        assert json.loads(read_regular(path)) == {"revision": 1 if phase == "before" else 2}
    assert json.loads(read_regular(path)) == {"revision": 1 if phase == "before" else 2}


@pytest.mark.integration
def test_killed_after_git_completion_does_not_infer_success_from_current_head(repo):
    # Make this a native fast-forward so the child cannot invoke a model.
    repo.git.run("checkout", "-B", "feature", repo.base_oid)
    script = """
import sys
from pathlib import Path
from git_ez.audit import Audit
from git_ez.config import Config
from git_ez.engine import Engine
def returned(*args):
    print('ready', flush=True)
    input()
    raise AssertionError('Parent must kill the writer')
Audit.returned = returned
Engine(Path(sys.argv[1]), Config(model='unused')).run('main', 'merge')
"""
    with paused_writer(script, repo.root):
        assert repo.git.text("rev-parse", "HEAD") == repo.parent_oid
    directory = next((repo.root / ".git/ez/runs").iterdir())
    view = load_audit(directory)
    assert view.exit_code == 1
    assert view.data["status"] == "running"
    assert not view.data["integrated"]
    assert "result_oid" not in view.data


@pytest.mark.unit
@pytest.mark.parametrize(
    "manifest",
    [b"not-json", b"[]", b'{"schema_version":999}', b'{"schema_version":1}', b"{}"],
)
def test_unreadable_manifest_never_falls_back_to_cached_success(tmp_path, manifest, capsys):
    (tmp_path / "run.json").write_bytes(manifest)
    (tmp_path / "report.md").write_text("Status: **succeeded**")
    assert main(["show", str(tmp_path)]) == 2
    output = capsys.readouterr()
    assert "succeeded" not in output.out
    assert "without trusting cached reports" in output.err


@pytest.mark.integration
@pytest.mark.parametrize("operation", ["merge", "rebase"])
def test_native_completion_survives_required_output_capture_failure(repo, operation):
    original = AuditStore.write

    def fail_output(self, relative, content, **kwargs):
        if relative.startswith("git-steps/") and relative.endswith(".stdout"):
            raise OSError("private provider token")
        return original(self, relative, content, **kwargs)

    # Use a conflict-free target to fail after a known completed native operation.
    with patch.object(AuditStore, "write", fail_output):
        audit = Engine(repo.root, repo.config, repo.resolver).run("HEAD", operation)
    assert audit.view["status"] == "failed"
    assert audit.view["integrated"]
    assert audit.view["result_oid"] == repo.source_oid
    assert not repo.git.operation_active()
    assert repo.git.run("status", "--porcelain").stdout == b""
    assert "private provider token" not in (audit.directory / "run.json").read_text()


@pytest.mark.integration
def test_result_diff_failure_does_not_prevent_independent_range_comparison(repo):
    original = AuditStore.write

    def fail_diff(self, relative, content, **kwargs):
        if relative == "result.diff":
            raise OSError("diff unavailable")
        return original(self, relative, content, **kwargs)

    with patch.object(AuditStore, "write", fail_diff):
        audit = Engine(repo.root, repo.config, repo.resolver).run("main", "rebase")
    assert audit.view["status"] == "failed"
    assert audit.view["integrated"]
    assert audit.published
    assert audit.has_artifact("range-diff.txt")
    assert audit.view["result_oid"] == repo.git.text("rev-parse", "HEAD")


@pytest.mark.integration
@pytest.mark.parametrize("damage", ["native_facts", "evidence_policy"])
def test_reader_rejects_success_without_consistent_facts(repo, damage):
    audit = Engine(repo.root, repo.config, repo.resolver).run("HEAD", "merge")
    assert audit.view["status"] == "succeeded"
    payload = json.loads((audit.directory / "run.json").read_bytes())
    if damage == "native_facts":
        payload["git"]["steps"] = []
    else:
        requirement = next(
            item for item in payload["requirements"] if item.get("artifact") == "result.diff"
        )
        requirement["requirement"] = "optional"
    (audit.directory / "run.json").write_text(json.dumps(payload))
    with pytest.raises(EzError, match="Cannot verify audit manifest"):
        load_audit(audit.directory)


@pytest.mark.integration
def test_failed_terminal_publication_cannot_be_hidden_by_stale_report(repo, capsys):
    original = AuditStore.snapshot

    def fail_terminal(self, payload):
        if payload["lifecycle"] == "finished":
            raise OSError("terminal write failed")
        return original(self, payload)

    with patch.object(AuditStore, "snapshot", fail_terminal):
        audit = Engine(repo.root, repo.config, repo.resolver).run("main", "merge")
    (audit.directory / "report.md").write_text("Status: **succeeded**")
    assert not audit.published
    assert audit.view["status"] == "failed"
    assert audit.view["integrated"]
    assert main(["show", str(audit.directory)]) == 1
    assert audit.view["failure"]["domain"] == "audit"
    output = capsys.readouterr()
    assert "Status: **succeeded**" not in output.out
    assert "not finalized" in output.err


@pytest.mark.integration
def test_reading_missing_evidence_keeps_history_but_refuses_verification(repo):
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", "merge")
    assert audit.view["status"] == "succeeded", audit.view
    assert load_audit(audit.directory).exit_code == 0
    (audit.directory / "result.diff").unlink()
    view = load_audit(audit.directory)
    assert view.exit_code == 1
    assert view.data["status"] == "succeeded"
    assert any("result.diff" in diagnostic for diagnostic in view.diagnostics)


@pytest.mark.integration
def test_reader_works_without_git_or_model_and_does_not_rewrite_history(repo):
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", "merge")
    original = (audit.directory / "run.json").read_bytes()
    repo.git.run("checkout", "main")
    with patch("git_ez.git.Git.run", side_effect=AssertionError("Reader executed Git")):
        assert main(["show", str(audit.directory)]) == 0
    assert (audit.directory / "run.json").read_bytes() == original


@pytest.mark.integration
def test_run_directory_collision_does_not_overwrite_another_run(repo):
    existing = repo.root / ".git/ez/runs/collision"
    existing.mkdir(parents=True)
    manifest = existing / "run.json"
    manifest.write_bytes(b"another run")
    with patch("git_ez.engine.uuid4", return_value=SimpleNamespace(hex="collision")):
        audit = Engine(repo.root, repo.config, repo.resolver).run("main", "merge")
    assert audit.view["status"] == "failed"
    assert not audit.published
    assert manifest.read_bytes() == b"another run"
    assert repo.git.branch_oid("feature") == repo.source_oid
    assert not repo.resolver.calls


@pytest.mark.integration
def test_optional_report_failure_preserves_published_core_success(repo):
    with patch("git_ez.audit.render_markdown", side_effect=ValueError("private rendering detail")):
        audit = Engine(repo.root, repo.config, repo.resolver).run("main", "merge")
    assert audit.view["status"] == "succeeded"
    assert audit.published
    assert audit.presentation_errors
    assert load_audit(audit.directory).exit_code == 0


@pytest.mark.integration
def test_proposal_copy_failure_blocks_staging_and_continuation(repo):
    original = AuditStore.write

    def fail_copy(self, relative, content, **kwargs):
        if relative.startswith("proposals/") and relative.endswith(".bin"):
            raise PermissionError("proposal copy denied")
        return original(self, relative, content, **kwargs)

    with patch.object(AuditStore, "write", fail_copy):
        audit = Engine(repo.root, repo.config, repo.resolver).run("main", "merge")
    assert audit.view["status"] == "failed"
    assert repo.git.conflicts()
    assert repo.git.branch_oid("feature") == repo.source_oid
    assert not any(
        step["argv"][0] == "add"
        for step in json.loads((audit.directory / "run.json").read_text())["git"]["steps"]
    )


@pytest.mark.integration
def test_untrusted_rationale_is_literal_in_audit_presentation(repo):
    original = repo.resolver.run

    def resolve(*args):
        result = original(*args)
        result["rationale"] = "![image](https://invalid.example/collect) <script>\\x1b[31m"
        return result

    with patch.object(repo.resolver, "run", resolve):
        audit = Engine(repo.root, repo.config, repo.resolver).run("main", "merge")
    output = (audit.directory / "report.md").read_text()
    assert "![image](https://" not in output
    assert "<script>" not in output
    assert "\x1b" not in output
