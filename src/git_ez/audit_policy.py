# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Expected evidence derived from admitted scopes, independently of captured files."""

from collections.abc import Mapping
from typing import Any

ROLES = frozenset(
    {
        "proof",
        "metadata_export",
        "git_step_stdout",
        "git_step_stderr",
        "conflict_input",
        "conflict_proposal",
        "file_content",
        "worktree_inventory",
        "worktree_diff",
        "event_stream",
        "staged_diff",
        "result_diff",
        "range_diff",
        "commit_log",
        "rebase_selection",
        "rebase_sequence",
        "sequence_digest",
        "sequence_metadata",
        "validation_output",
        "final_inventory",
    }
)

# Prefixes are accepted only where the existing capture policy defines a bound.
# Inventory truncation describes bounded children, never a truncated JSON document.
PREFIX_ROLES = frozenset(
    {
        "file_content",
        "worktree_inventory",
        "final_inventory",
        "commit_log",
        "rebase_selection",
        "metadata_export",
    }
)


def capture_satisfies(entry: Mapping[str, Any]) -> bool:
    """Whether retained content satisfies this role's capture policy."""
    if entry.get("status") == "unavailable" or entry.get("result") in (
        "not_attempted",
        "io_error",
        "observation_error",
        "invalid",
        "interrupted",
        "unsupported",
    ):
        return False
    if entry.get("truncated") or entry.get("result") == "truncated":
        return entry.get("role") in PREFIX_ROLES
    return True


def artifact_owner(path: str, data: Mapping[str, Any]) -> str:
    for group in ("sessions", "reviews", "native_resolutions"):
        for record in data.get(group, []):
            prefix = record.get("artifacts")
            if isinstance(prefix, str) and path.startswith(prefix.rstrip("/") + "/"):
                return str(record["id"])
            if record.get("staged_artifact") == path:
                return str(record["id"])
    for conflict in data.get("conflicts", []):
        if path.startswith(f"conflicts/{conflict['number']:04d}/"):
            return str(conflict["id"])
    return str(data.get("id", "run"))


def required_artifacts(data: Mapping[str, Any]) -> dict[str, str]:
    """One boundary policy for the writer, snapshot validator and offline reader.

    Facts make a proof due. A missing registry entry cannot remove that obligation.
    Pending scopes may publish incomplete checkpoints, but cannot authorize their
    next mutation or a successful terminal outcome.
    """
    required: dict[str, str] = {}
    for index, step in enumerate(data.get("steps", []), 1):
        if step.get("result") == "returned":
            for channel in ("stdout", "stderr"):
                required[f"git-steps/{index:04d}.{channel}"] = f"git_step_{channel}"
    for conflict in data.get("conflicts", []):
        folder = f"conflicts/{conflict['number']:04d}"
        # Unsupported inputs are recorded as such; supported stages must have bytes.
        if conflict.get("input_status") != "unsupported":
            for stage, label in (("1", "base"), ("2", "ours"), ("3", "theirs")):
                if stage in conflict.get("stages", {}):
                    required[f"{folder}/{label}.txt"] = "conflict_input"
            required[f"{folder}/request.json"] = "conflict_input"
            if conflict.get("marked_present"):
                required[f"{folder}/marked.txt"] = "conflict_input"
        if conflict.get("application") == "staged":
            path = conflict.get("artifacts", {}).get("staged")
            if path:
                required[path] = "staged_diff"
    for proposal in data.get("reviews", []):
        if "resolution" in proposal:
            folder = proposal["artifacts"]
            required[f"{folder}/files.json"] = "worktree_inventory"
            required[f"{folder}/worktree.diff"] = "worktree_diff"
    for session in data.get("sessions", []):
        if session.get("invocation_started"):
            required[f"{session['artifacts']}/events.jsonl"] = "event_stream"
        if session.get("status") not in ("created", "running", "pending"):
            required[f"{session['artifacts']}/files.json"] = "worktree_inventory"
            required[f"{session['artifacts']}/worktree.diff"] = "worktree_diff"
    for resolution in data.get("native_resolutions", []):
        required[resolution["staged_artifact"]] = "staged_diff"
    if data.get("integrated"):
        required["result.diff"] = "result_diff"
        if data.get("operation") == "rebase":
            required["range-diff.txt"] = "range_diff"
            if "rebase_sequence" in data:
                required["rebase-commits.log"] = "commit_log"
    anchor = data.get("rebase_plan", {}).get("selection_anchor")
    if anchor:
        required[anchor["artifact"]] = "rebase_selection"
    sequence = data.get("rebase_sequence")
    if sequence and sequence.get("status") != "pending":
        required[sequence["artifact"]] = "rebase_sequence"
        required[sequence["digest_artifact"]] = "sequence_digest"
        required[sequence["metadata_artifact"]] = "sequence_metadata"
    validation = data.get("validation", {})
    if "returncode" in validation or validation.get("status") == "timed_out":
        required["validation.stdout"] = "validation_output"
        required["validation.stderr"] = "validation_output"
    if data.get("lifecycle") in ("finalizing", "finished") and data.get("steps"):
        required["exit/files.json"] = "final_inventory"
        required["exit/worktree.diff"] = "worktree_diff"
    return required


def missing_requirements(
    data: Mapping[str, Any], evidence: Mapping[str, Mapping[str, Any]]
) -> tuple[str, ...]:
    return tuple(
        path
        for path in required_artifacts(data)
        if path not in evidence or evidence[path].get("status") == "unavailable"
    )


def requirement_records(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Include due-but-absent proofs, not just the captures that happened to work."""
    expected = required_artifacts(data)
    artifacts = data.get("evidence", {})
    records: list[dict[str, Any]] = [
        {
            "id": name,
            "role": "core_fact",
            "requirement": "blocking",
            "applicability": "required",
            "fact": name,
        }
        for name in ("repository", "request", "inputs", "git", "checks")
    ]
    for path in sorted(set(expected) | set(artifacts)):
        entry = artifacts.get(path, {})
        requirement = "blocking" if path in expected else entry.get("requirement", "blocking")
        records.append(
            {
                "id": path,
                "role": expected.get(path, entry.get("role", "proof")),
                "requirement": requirement,
                "applicability": "not_applicable"
                if entry.get("result") == "not_attempted" and requirement == "optional"
                else "required",
                "artifact": path,
                "owner": artifact_owner(path, data),
                "reason": entry.get("reason"),
            }
        )
    return records
