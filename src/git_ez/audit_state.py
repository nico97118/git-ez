# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Audit facts, immutable exported views and schema invariants."""

import copy
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, NotRequired, TypedDict

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
        any(
            entry.get("role") == "conflict_proposal" and entry.get("truncated")
            for entry in evidence.values()
        )
        or any(observation.get("errors") for observation in observations)
        or any(issue["blocking"] for issue in capture_issues)
        or any(
            entry.get("requirement", "blocking") == "blocking"
            and (
                entry["status"] == "unavailable"
                or entry.get("result")
                in ("not_attempted", "io_error", "observation_error", "invalid", "interrupted")
            )
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

    def health(self) -> str:
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
            "checks": {"validation": validation, "final": {"status": self.final_checks}},
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
        payload["requirements"] = [
            {
                "id": name,
                "role": "core_fact",
                "requirement": "blocking",
                "applicability": "required",
                "fact": name,
            }
            for name in ("repository", "request", "inputs", "git", "checks")
        ] + [
            {
                "id": path,
                "role": entry.get("role", "proof"),
                "requirement": entry.get("requirement", "blocking"),
                "applicability": "not_applicable"
                if entry.get("result") == "not_attempted" and entry.get("requirement") == "optional"
                else "required",
                "artifact": path,
                "reason": entry.get("reason"),
            }
            for path, entry in self.evidence.items()
        ]
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
        conflicts=payload["conflicts"],
        sessions=payload["sessions"],
        reviews=payload["proposals"],
        evidence=payload["artifacts"],
        audit_errors=[issue for issue in payload["issues"] if issue.get("domain") != "execution"],
        audit_health=payload["audit_health"],
    )
    if payload["git"]["result_oid"] is not None:
        data["result_oid"] = payload["git"]["result_oid"]
    if payload["primary_failure"] is not None:
        data.update(failure=payload["primary_failure"], error=payload["primary_failure"]["message"])
    return data


def validate_snapshot(payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != 2:
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
    for entry in payload["artifacts"].values():
        if (
            entry.get("requirement") == "optional"
            and entry.get("role") not in ("metadata_export",)
            and entry.get("result") != "not_attempted"
        ):
            raise ValueError("A mandatory evidence role cannot be made optional")
        if "sha256" in entry:
            digest = entry["sha256"]
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
            ):
                raise ValueError("Invalid artifact hash")
            if type(entry.get("retained_bytes")) is not int or entry["retained_bytes"] < 0:
                raise ValueError("Invalid retained evidence size")
        elif entry["status"] != "unavailable":
            raise ValueError("Retained evidence lacks its fingerprint")
    if payload.get("audit_health") != evidence_health(
        payload["artifacts"], payload["issues"], payload["git"]["observations"]
    ):
        raise ValueError("Evidence health disagrees with its recorded facts")
    if not isinstance(payload.get("requirements"), list):
        raise ValueError("Missing evidence requirements")
    represented = {
        requirement.get("artifact")
        for requirement in payload["requirements"]
        if isinstance(requirement, dict)
    }
    if represented - {None} != set(payload["artifacts"]):
        raise ValueError("Evidence registry and requirements disagree")
    for requirement in payload["requirements"]:
        if not isinstance(requirement, dict):
            raise ValueError("Invalid evidence requirement record")
        if "artifact" in requirement:
            entry = payload["artifacts"][requirement["artifact"]]
            if requirement.get("role") != entry.get("role", "proof") or requirement.get(
                "requirement"
            ) != entry.get("requirement", "blocking"):
                raise ValueError("Evidence policy disagrees with its requirement")
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
