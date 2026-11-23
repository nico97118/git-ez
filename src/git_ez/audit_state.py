# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Audit facts, immutable exported views and schema invariants."""

import copy
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, NotRequired, TypedDict

from .audit_policy import (
    ROLES,
    artifact_owner,
    capture_satisfies,
    missing_requirements,
    required_artifacts,
    requirement_records,
)

RunStatus = Literal["running", "succeeded", "failed", "interrupted"]
IntegrationStatus = Literal["not_started", "in_progress", "completed", "not_completed", "unknown"]
ValidationStatus = Literal[
    "not_requested", "not_run", "running", "passed", "failed", "timed_out", "interrupted", "unknown"
]


class Failure(TypedDict):
    phase: str
    type: str
    message: str
    artifact: NotRequired[str]
    id: NotRequired[str]
    domain: NotRequired[str]
    owner: NotRequired[str]
    blocking: NotRequired[bool]


class Evidence(TypedDict):
    status: Literal["available", "partial", "unavailable"]
    reason: NotRequired[str]
    requirement: NotRequired[str]
    result: NotRequired[str]
    retained_bytes: NotRequired[int]
    sha256: NotRequired[str]
    prefix: NotRequired[bool]
    capture_id: NotRequired[str]
    role: NotRequired[str]
    source_bytes: NotRequired[int]
    redacted: NotRequired[bool]
    truncated: NotRequired[bool]
    id: NotRequired[str]
    owner: NotRequired[str]
    captured_at: NotRequired[str]
    source_observation: NotRequired[str]
    records: NotRequired[int]


class Validation(TypedDict):
    command: list[str]
    status: ValidationStatus
    returncode: NotRequired[int]


def now() -> str:
    return datetime.now(UTC).isoformat()


def safe_error(error: BaseException, secrets: tuple[str, ...] = ()) -> str:
    # Only project-owned errors have user-facing, controlled messages. Never persist
    # arbitrary SDK exception bodies or representations as a fallback.
    from .errors import EzError

    if isinstance(error, EzError):
        message = str(error)
    elif isinstance(error, OSError):
        message = f"{type(error).__name__}: audit I/O failed"
        if error.errno is not None:
            message += f" (errno {error.errno})"
    elif isinstance(error, KeyboardInterrupt):
        message = "Interrupted by user"
    else:
        message = f"Unexpected {type(error).__name__}"
    for secret in secrets:
        if secret:
            message = message.replace(secret, "<redacted>")
    return message


def evidence_health(
    evidence: Mapping[str, Mapping[str, Any]],
    issues: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
) -> str:
    """Apply the same retained-evidence policy to live and serialized facts."""
    capture_issues = [issue for issue in issues if "blocking" in issue]
    if (
        any(observation.get("errors") for observation in observations)
        or any(issue["blocking"] for issue in capture_issues)
        or any(
            entry.get("requirement", "blocking") == "blocking" and not capture_satisfies(entry)
            for entry in evidence.values()
        )
    ):
        return "incomplete"
    if capture_issues or any(
        entry["status"] != "available" and entry.get("result") != "not_attempted"
        for entry in evidence.values()
    ):
        return "degraded"
    return "complete"


@dataclass(frozen=True)
class PinnedRef:
    oid: str
    reference: str | None


@dataclass(frozen=True)
class PinnedInputs:
    branch: str
    before_oid: str
    target: PinnedRef
    onto: PinnedRef | None


@dataclass
class RunState:
    """Mutable only inside the run owner; exported payloads are defensive copies."""

    run_id: str
    details: dict[str, Any]
    revision: int = 0
    lifecycle: str = "preparing"
    status: RunStatus = "running"
    integration: IntegrationStatus = "not_started"
    result_oid: str | None = None
    final_checks: str = "not_run"
    failure: Failure | None = None
    audit_errors: list[Failure] = field(default_factory=list)
    issues: list[Failure] = field(default_factory=list)
    evidence: dict[str, Evidence] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)

    def policy_data(self) -> dict[str, Any]:
        return {
            **self.details,
            "id": self.run_id,
            "steps": self.steps,
            "lifecycle": self.lifecycle,
            "integrated": self.integration == "completed",
        }

    def health(self) -> str:
        if missing_requirements(self.policy_data(), self.evidence):
            return "incomplete"
        return evidence_health(self.evidence, self.audit_errors, self.observations)

    def view(self) -> dict[str, Any]:
        data = copy.deepcopy(self.details)
        data.update(
            schema_version=2,
            id=self.run_id,
            revision=self.revision,
            lifecycle=self.lifecycle,
            status=self.status,
            integrated=self.integration == "completed",
            integration=self.integration,
            final_checks=self.final_checks,
            audit_health=self.health(),
            audit_errors=copy.deepcopy(self.audit_errors),
            evidence=copy.deepcopy(self.evidence),
        )
        if self.result_oid is not None:
            data["result_oid"] = self.result_oid
        if self.failure is not None:
            data.update(failure=dict(self.failure), error=self.failure["message"])
        return data

    def snapshot(self) -> dict[str, Any]:
        data = self.view()
        inputs = {
            key: data.pop(key)
            for key in (
                "branch",
                "before_oid",
                "target_oid",
                "target_ref",
                "onto_oid",
                "onto_ref",
                "rebase_plan",
            )
            if key in data
        }
        request = {
            key: data.pop(key)
            for key in (
                "operation",
                "target",
                "onto",
                "provider",
                "model",
                "min_confidence",
                "configuration",
                "prompt",
                "rebase_options",
                "commit_options",
                "merge_ff",
                "rerere_autoupdate",
                "interactive",
            )
            if key in data
        }
        repository = {"worktree": data.pop("worktree")}
        for key in ("git_directory", "common_git_directory"):
            if key in data:
                repository[key] = data.pop(key)
        validation = data.pop("validation", {"command": [], "status": "not_requested"})
        payload = {
            "schema_version": 2,
            "run_id": self.run_id,
            "revision": self.revision,
            "created_at": data.pop("created_at"),
            "updated_at": now(),
            "lifecycle": self.lifecycle,
            "outcome": "pending" if self.lifecycle != "finished" else self.status,
            "repository": repository,
            "request": request,
            "inputs": inputs,
            "git": {
                "integration": self.integration,
                "result_oid": self.result_oid,
                "steps": copy.deepcopy(self.steps),
                "observations": copy.deepcopy(self.observations),
            },
            "checks": {
                "validation": validation,
                "final": {
                    "status": self.final_checks,
                    "records": data.pop("final_check_records", []),
                },
            },
            "conflicts": data.pop("conflicts", []),
            "sessions": data.pop("sessions", []),
            "proposals": data.pop("reviews", []),
            "artifacts": copy.deepcopy(self.evidence),
            "issues": copy.deepcopy(self.issues),
            "primary_failure": copy.deepcopy(self.failure),
            "audit_health": self.health(),
        }
        for key in (
            "schema_version",
            "id",
            "revision",
            "lifecycle",
            "status",
            "integrated",
            "integration",
            "final_checks",
            "audit_health",
            "audit_errors",
            "evidence",
            "failure",
            "error",
            "result_oid",
        ):
            data.pop(key, None)
        payload["requirements"] = requirement_records(data=self.view() | {"steps": self.steps})
        payload["primary_failure_id"] = self.failure.get("id") if self.failure else None
        payload["details"] = data
        validate_snapshot(payload)
        return payload


def snapshot_view(payload: dict[str, Any]) -> dict[str, Any]:
    """One read model for engine inspection and all derived presentations."""
    data: dict[str, Any] = copy.deepcopy(payload.get("details", {}))
    data.update(payload["request"])
    data.update(payload["inputs"])
    data.update(payload["repository"])
    data.update(
        id=payload["run_id"],
        schema_version=2,
        revision=payload["revision"],
        created_at=payload["created_at"],
        updated_at=payload["updated_at"],
        lifecycle=payload["lifecycle"],
        status="running" if payload["outcome"] == "pending" else payload["outcome"],
        integrated=payload["git"]["integration"] == "completed",
        integration=payload["git"]["integration"],
        validation=payload["checks"]["validation"],
        final_checks=payload["checks"]["final"]["status"],
        final_check_records=payload["checks"]["final"].get("records", []),
        conflicts=payload["conflicts"],
        sessions=payload["sessions"],
        reviews=payload["proposals"],
        evidence=payload["artifacts"],
        steps=payload["git"]["steps"],
        audit_errors=[issue for issue in payload["issues"] if issue.get("domain") != "execution"],
        audit_health=payload["audit_health"],
    )
    if payload["git"]["result_oid"] is not None:
        data["result_oid"] = payload["git"]["result_oid"]
    if payload["primary_failure"] is not None:
        data.update(failure=payload["primary_failure"], error=payload["primary_failure"]["message"])
    return data


def _strings(record: dict[str, Any], *fields: str) -> None:
    if any(not isinstance(record.get(field), str) or not record[field] for field in fields):
        raise ValueError("Missing required audit facts")


def _oid(value: object) -> None:
    if (
        not isinstance(value, str)
        or len(value) not in (40, 64)
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("Invalid full native object identity")


def validate_scope(group: str, record: dict[str, Any]) -> None:
    """Reject malformed layouts before they can enter the owned aggregate."""
    _strings(record, "id")
    if group in ("sessions", "reviews", "proposals"):
        _strings(record, "status", "artifacts")
        if type(record.get("round")) is not int:
            raise ValueError("Invalid scope relationship")
    elif group == "conflicts":
        _strings(record, "path", "status")
        if type(record.get("number")) is not int or not isinstance(record.get("stages"), dict):
            raise ValueError("Invalid conflict provenance")
        if not isinstance(record.get("artifacts"), dict):
            raise ValueError("Invalid conflict evidence references")
    elif group == "native_resolutions":
        _strings(record, "staged_artifact")


def validate_records(payload: dict[str, Any]) -> None:
    """Validate child shapes before projecting or evaluating their policy."""
    _strings(payload["repository"], "worktree")
    _strings(payload["request"], "operation", "target", "provider", "model", "prompt")
    if payload["request"]["operation"] not in ("merge", "rebase") or not isinstance(
        payload["request"].get("configuration"), dict
    ):
        raise ValueError("Invalid admitted request")
    _strings(payload["inputs"], "branch", "before_oid", "target_oid")
    for name in ("before_oid", "target_oid", "onto_oid"):
        if name in payload["inputs"]:
            _oid(payload["inputs"][name])
    if payload["git"].get("integration") == "completed":
        _oid(payload["git"].get("result_oid"))
    for group in ("steps", "observations"):
        records = payload["git"].get(group)
        if not isinstance(records, list):
            raise ValueError("Invalid native record inventory")
        identifiers: set[str] = set()
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("Invalid native record")
            _strings(record, "id")
            if record["id"] in identifiers:
                raise ValueError("Duplicate native record identity")
            identifiers.add(record["id"])
            if group == "steps":
                if (
                    record.get("purpose") not in ("stage", "integration")
                    or record.get("result")
                    not in (
                        "pending",
                        "returned",
                        "unknown",
                        "not_launched",
                    )
                    or not isinstance(record.get("argv"), list)
                ):
                    raise ValueError("Invalid native command facts")
                if record["result"] == "returned" and type(record.get("returncode")) is not int:
                    raise ValueError("Missing native return code")
            elif not isinstance(record.get("errors"), list) or any(
                not isinstance(error, dict) or not isinstance(error.get("field"), str)
                for error in record["errors"]
            ):
                raise ValueError("Invalid native observation errors")
    for group in ("native_resolutions", "empty_commits"):
        records = payload["details"].get(group, [])
        if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
            raise ValueError("Invalid native scope facts")
        for record in records:
            validate_scope(group, record)
    sequence = payload["details"].get("rebase_sequence")
    if sequence is not None:
        if not isinstance(sequence, dict):
            raise ValueError("Invalid sequence capture facts")
        _strings(sequence, "artifact", "digest_artifact", "metadata_artifact", "status")
    for group in ("conflicts", "sessions", "proposals", "issues"):
        scope_identifiers: set[str] = set()
        for record in payload[group]:
            if not isinstance(record, dict):
                raise ValueError("Invalid scoped audit record")
            _strings(record, "id")
            validate_scope(group, record)
            if record["id"] in scope_identifiers:
                raise ValueError("Duplicate scoped audit identity")
            scope_identifiers.add(record["id"])
            if group == "issues":
                _strings(record, "domain", "owner", "phase", "type", "message")
                if record["domain"] not in ("audit", "execution", "observation"):
                    raise ValueError("Invalid failure domain")
            elif group == "conflicts":
                _strings(record, "path", "status")
                if type(record.get("number")) is not int or not isinstance(
                    record.get("stages"), dict
                ):
                    raise ValueError("Invalid conflict provenance")
                if not isinstance(record.get("artifacts"), dict):
                    raise ValueError("Invalid conflict evidence references")
            else:
                _strings(record, "status", "artifacts")
                if type(record.get("round")) is not int:
                    raise ValueError("Invalid scope relationship")
                if (
                    group == "sessions"
                    and record.get("invocation_started")
                    and (
                        not isinstance(record.get("runtime_metadata"), dict)
                        or not isinstance(record.get("mission"), dict)
                    )
                ):
                    raise ValueError("Missing model invocation provenance")
                resolution = record.get("resolution")
                if resolution is not None and (
                    not isinstance(resolution, dict)
                    or not isinstance(resolution.get("rationale"), str)
                    or type(resolution.get("confidence")) not in (int, float)
                    or not 0 <= resolution["confidence"] <= 1
                ):
                    raise ValueError("Invalid proposal decision")
            if group != "issues" and record.get("status") not in (
                "created",
                "pending",
                "running",
                "resolved",
                "proposed",
                "applied",
                "failed",
                "interrupted",
                "low_confidence",
                "unknown",
            ):
                raise ValueError("Invalid scope outcome")
            if payload["lifecycle"] == "finished" and record.get("status") in (
                "created",
                "pending",
                "running",
            ):
                raise ValueError("A finished run cannot retain an open scope")
    failure = payload.get("primary_failure")
    if failure is not None:
        if not isinstance(failure, dict) or failure not in payload["issues"]:
            raise ValueError("Primary failure is not in the issue ledger")
        if payload.get("primary_failure_id") != failure["id"]:
            raise ValueError("Invalid primary failure reference")
    elif payload.get("primary_failure_id") is not None:
        raise ValueError("Primary failure reference has no fact")
    checks = payload["checks"]
    if (
        not isinstance(checks.get("final"), dict)
        or checks["final"].get("status") not in ("not_run", "passed", "failed", "unknown")
        or not isinstance(checks.get("validation"), dict)
    ):
        raise ValueError("Invalid check facts")
    validation = checks["validation"]
    command = validation.get("command")
    if not isinstance(command, list) or any(not isinstance(arg, str) for arg in command):
        raise ValueError("Invalid validation invocation")
    code = validation.get("returncode")
    if code is not None and type(code) is not int:
        raise ValueError("Invalid validation return code")
    if validation.get("status") == "passed" and (code != 0 or not command):
        raise ValueError("Validation success contradicts its native return")
    if validation.get("status") == "not_requested" and command:
        raise ValueError("Requested validation cannot be marked not requested")
    if validation.get("status") == "not_requested" and code is not None:
        raise ValueError("Unrequested validation cannot have a native return")
    if code == 0 and validation.get("status") == "failed":
        raise ValueError("Validation failure contradicts its native return")
    conflict_ids = {record["id"] for record in payload["conflicts"]}
    proposal_ids = {record["id"] for record in payload["proposals"]}
    steps_by_id = {step["id"]: step for step in payload["git"]["steps"]}
    for record in [*payload["proposals"], *payload["sessions"]]:
        references = record.get("conflict_ids", [])
        if not isinstance(references, list) or any(ref not in conflict_ids for ref in references):
            raise ValueError("Unknown conflict scope reference")
        if record.get("invocation_started") and record.get("proposal_id") not in proposal_ids:
            raise ValueError("Unknown parent proposal for model invocation")
    for record in [*payload["conflicts"], *payload["proposals"]]:
        if record.get("application") == "staged":
            step = steps_by_id.get(record.get("stage_step_id"))
            if step is None or step.get("purpose") != "stage" or step.get("returncode") != 0:
                raise ValueError("Staged application lacks its successful stage step")


def validate_snapshot(payload: dict[str, Any]) -> None:
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 2:
        raise ValueError("Unsupported audit schema")
    if not isinstance(payload.get("run_id"), str) or not payload["run_id"]:
        raise ValueError("Missing audit identity")
    if type(payload.get("revision")) is not int or payload["revision"] < 1:
        raise ValueError("Invalid audit revision")
    if payload.get("lifecycle") not in ("preparing", "active", "finalizing", "finished"):
        raise ValueError("Invalid audit lifecycle")
    if payload.get("outcome") not in ("pending", "succeeded", "failed", "interrupted"):
        raise ValueError("Invalid audit outcome")
    if (payload["lifecycle"] == "finished") == (payload["outcome"] == "pending"):
        raise ValueError("Inconsistent audit finalization")
    for key in ("repository", "request", "inputs", "git", "checks", "artifacts", "details"):
        if not isinstance(payload.get(key), dict):
            raise ValueError(f"Invalid audit {key}")
    for key in ("conflicts", "sessions", "proposals", "issues"):
        if not isinstance(payload.get(key), list):
            raise ValueError(f"Invalid audit {key}")
    validate_records(payload)
    integration = payload["git"]["integration"]
    if integration not in ("not_started", "in_progress", "completed", "not_completed", "unknown"):
        raise ValueError("Invalid Git integration outcome")
    for key in ("steps", "observations"):
        if not isinstance(payload["git"].get(key), list) or any(
            not isinstance(item, dict) for item in payload["git"][key]
        ):
            raise ValueError("Invalid Git evidence inventory")
    validation = payload["checks"]["validation"]
    if validation["status"] not in (
        "not_requested",
        "not_run",
        "running",
        "passed",
        "failed",
        "timed_out",
        "interrupted",
        "unknown",
    ) or not isinstance(validation["command"], list):
        raise ValueError("Invalid validation facts")
    if payload["outcome"] == "succeeded" and (
        integration != "completed"
        or not isinstance(payload["git"]["result_oid"], str)
        or payload["checks"]["final"]["status"] != "passed"
        or validation["status"] not in ("passed", "not_requested")
        or payload["primary_failure"] is not None
        or payload["audit_health"] == "incomplete"
    ):
        raise ValueError("Audit success is not supported by its facts")
    for path, entry in payload["artifacts"].items():
        if not isinstance(path, str) or not isinstance(entry, dict):
            raise ValueError("Invalid audit artifact")
        if entry["status"] not in ("available", "partial", "unavailable"):
            raise ValueError("Invalid evidence status")
        if entry.get("requirement", "blocking") not in ("blocking", "optional"):
            raise ValueError("Invalid evidence requirement")
        if entry.get("role", "proof") not in ROLES or entry.get("result") not in (
            "not_attempted",
            "ok",
            "truncated",
            "redacted",
            "unsupported",
            "io_error",
            "observation_error",
            "invalid",
            "interrupted",
        ):
            raise ValueError("Unknown evidence policy or capture result")
    for entry in payload["artifacts"].values():
        if (
            entry.get("requirement") == "optional"
            and entry.get("role") not in ("metadata_export",)
            and entry.get("result") != "not_attempted"
        ):
            raise ValueError("A mandatory evidence role cannot be made optional")
        if "sha256" in entry:
            _strings(entry, "id", "owner", "captured_at", "capture_id")
            digest = entry["sha256"]
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
            ):
                raise ValueError("Invalid artifact hash")
            if type(entry.get("retained_bytes")) is not int or entry["retained_bytes"] < 0:
                raise ValueError("Invalid retained evidence size")
            if "source_bytes" in entry and (
                type(entry["source_bytes"]) is not int or entry["source_bytes"] < 0
            ):
                raise ValueError("Invalid observed source size")
            if "prefix" in entry and type(entry["prefix"]) is not bool:
                raise ValueError("Invalid stream prefix policy")
            if entry.get("role") == "event_stream" and (
                entry.get("prefix") is not True
                or type(entry.get("records")) is not int
                or entry["records"] < 1
            ):
                raise ValueError("Invalid acknowledged event records")
        elif entry["status"] != "unavailable":
            raise ValueError("Retained evidence lacks its fingerprint")
    view = snapshot_view(payload)
    if any(
        "sha256" in entry and entry.get("owner") != artifact_owner(path, view)
        for path, entry in payload["artifacts"].items()
    ):
        raise ValueError("Capture ownership contradicts its admitted scope")
    expected_health = (
        "incomplete"
        if missing_requirements(view, payload["artifacts"])
        else evidence_health(
            payload["artifacts"], payload["issues"], payload["git"]["observations"]
        )
    )
    if payload.get("audit_health") != expected_health:
        raise ValueError("Evidence health disagrees with its recorded facts")
    if payload.get("requirements") != requirement_records(view):
        raise ValueError("Evidence registry and expected requirements disagree")
    for path, role in required_artifacts(view).items():
        entry = payload["artifacts"].get(path)
        if entry and (entry.get("requirement") != "blocking" or entry.get("role") != role):
            raise ValueError("An expected proof cannot weaken its policy role")
    if payload["outcome"] == "succeeded":
        required_roles = {"result_diff", "final_inventory", "git_step_stdout", "git_step_stderr"}
        if payload["request"]["operation"] == "rebase":
            required_roles.add("range_diff")
        roles = {
            entry.get("role")
            for entry in payload["artifacts"].values()
            if entry["status"] != "unavailable"
        }
        if not required_roles <= roles:
            raise ValueError("Audit success lacks its mandatory evidence inventory")
        observations = {item["id"] for item in payload["git"]["observations"]}
        steps = payload["git"]["steps"]
        if not steps or any(
            step.get("result") != "returned"
            or type(step.get("returncode")) is not int
            or step.get("before") not in observations
            or step.get("after") not in observations
            or any(
                step.get(channel + "_artifact") not in payload["artifacts"]
                for channel in ("stdout", "stderr")
            )
            for step in steps
        ):
            raise ValueError("Audit success lacks its native command facts")
        positions = {
            record["id"]: index for index, record in enumerate(payload["git"]["observations"])
        }
        if any(
            step["stdout_artifact"] != f"git-steps/{index:04d}.stdout"
            or step["stderr_artifact"] != f"git-steps/{index:04d}.stderr"
            or positions[step["before"]] >= positions[step["after"]]
            for index, step in enumerate(steps, 1)
        ):
            raise ValueError("Native evidence references are inconsistent")
        integrations = [step for step in steps if step.get("purpose") == "integration"]
        if not integrations or integrations[-1]["returncode"] != 0:
            raise ValueError("Audit success lacks a successful final integration step")
        completion = next(
            item
            for item in payload["git"]["observations"]
            if item["id"] == integrations[-1]["after"]
        )
        if (
            completion.get("head") != payload["git"]["result_oid"]
            or completion.get("operation_active") is not False
        ):
            raise ValueError("Completion contradicts its native observation")
        facts = payload["details"].get("final_check_facts", {})
        if (
            facts.get("result_oid") != payload["git"]["result_oid"]
            or facts.get("branch") != payload["inputs"]["branch"]
        ):
            raise ValueError("Final checks do not identify the verified result")
        checks = payload["checks"]["final"].get("records", [])
        names = {record["name"] for record in checks if isinstance(record, dict)}
        if not {
            "HEAD",
            "clean_checkout",
            "unmerged_index",
            "operation_active",
            "branch_identity",
            "branch_tip",
            "target_ancestry",
        } <= names or any(
            not isinstance(record, dict)
            or record.get("status") != "passed"
            or record.get("observed") != record.get("expected")
            for record in checks
        ):
            raise ValueError("Final verification lacks its individual observations")
    # JSON validation also rejects non-finite numbers and accidental SDK objects.
    json.dumps(payload, allow_nan=False)


@dataclass(frozen=True)
class AuditView:
    payload: bytes
    diagnostics: tuple[str, ...] = ()
    exit_code: int = 0

    @property
    def data(self) -> dict[str, Any]:
        decoded = json.loads(self.payload)
        return snapshot_view(decoded)


@dataclass(frozen=True)
class AuditResult:
    """Frozen live result; publication acknowledgement is separate from core facts."""

    payload: bytes
    publication: Literal["published", "failed", "unknown"]
    attempted_revision: int
    published_revision: int | None

    @property
    def view(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.payload)
        return data
