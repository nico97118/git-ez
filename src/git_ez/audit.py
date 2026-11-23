# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Single-owner run state, evidence gates and independent audit finalization."""

import copy
import hashlib
import json
import os
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from threading import RLock
from typing import Any, Literal
from uuid import UUID, uuid4

from .audit_capture import capture_worktree, observe
from .audit_policy import artifact_owner, required_artifacts
from .audit_report import render_markdown
from .audit_safety import redact_bytes, redact_chunks
from .audit_state import (
    AuditResult,
    Evidence,
    Failure,
    PinnedInputs,
    PinnedRef,
    RunState,
    RunStatus,
    now,
    safe_error,
    validate_scope,
)
from .audit_storage import AuditStore, directory_fd, encode_json, fingerprint, regular_fd
from .errors import AuditError, EzError
from .git import Git


class Record(Mapping[str, Any]):
    """A scoped record handle; only its owner updates the backing state."""

    def __init__(self, audit: "Audit", group: str, index: int):
        self._audit = audit
        self._group = group
        self._index = index

    def __getitem__(self, key: str) -> Any:
        with self._audit._lock:
            return copy.deepcopy(self._audit._state.details[self._group][self._index][key])

    def __iter__(self) -> Iterator[str]:
        return iter(self._audit.view[self._group][self._index])

    def __len__(self) -> int:
        return len(self._audit.view[self._group][self._index])

    def update(self, *args: Mapping[str, Any], **values: Any) -> None:
        data = dict(args[0]) if args else {}
        data.update(values)
        self._audit.update_record(self._group, self._index, data)

    def artifact(self, label: str, path: str) -> None:
        data = self.get("artifacts", {})
        data[label] = path
        self.update(artifacts=data)


class Audit:
    """One run. Construction has no I/O; initialize inside the handled-exit boundary."""

    def __init__(self, directory: Path, metadata: dict[str, Any]):
        self.directory = directory
        self._lock = RLock()
        self._store = AuditStore(directory)
        data = copy.deepcopy(metadata)
        data.setdefault("created_at", now())
        data.pop("schema_version", None)
        data.pop("status", None)
        data.pop("integrated", None)
        self._state = RunState(directory.name, data)
        self._secrets: tuple[str, ...] = ()
        self._published_revision: int | None = None
        self._published_bytes: bytes | None = None
        self._finalized = False
        self._closing = False
        self._result: AuditResult | None = None
        self._blocked_streams: set[str] = set()
        self._stream_hashes: dict[str, Any] = {}
        self.presentation_errors: list[str] = []

    @property
    def inputs(self) -> PinnedInputs:
        with self._lock:
            data = self._state.details
            onto = (
                PinnedRef(data["onto_oid"], data.get("onto_ref")) if data.get("onto_oid") else None
            )
            return PinnedInputs(
                data["branch"],
                data["before_oid"],
                PinnedRef(data["target_oid"], data.get("target_ref")),
                onto,
            )

    @property
    def operation(self) -> str:
        return str(self._state.details["operation"])

    @property
    def view(self) -> dict[str, Any]:
        with self._lock:
            return self._result.view if self._result is not None else self._state.view()

    @property
    def evidence(self) -> dict[str, Evidence]:
        with self._lock:
            return copy.deepcopy(self._state.evidence)

    @property
    def audit_errors(self) -> list[Failure]:
        with self._lock:
            return copy.deepcopy(self._state.audit_errors)

    @property
    def published(self) -> bool:
        return self._result is not None and self._result.publication == "published"

    @property
    def terminal_result(self) -> AuditResult | None:
        return self._result

    def protect(self, *secrets: str) -> None:
        with self._lock:
            self._ensure_open()
            self._secrets = tuple(
                sorted({secret for secret in secrets if secret}, key=len, reverse=True)
            )

    def _redact(self, data: Any, *, enabled: bool = True) -> Any:
        """Redact content values, never format keys or arbitrary object reprs."""
        if isinstance(data, str):
            if not enabled:
                return data
            for secret in self._secrets:
                data = "<redacted>".join(
                    part.replace(secret, "<redacted>") for part in data.split("<redacted>")
                )
            return data
        if isinstance(data, dict):
            return {key: self._redact(value, enabled=enabled) for key, value in data.items()}
        if isinstance(data, (list, tuple)):
            return [self._redact(value, enabled=enabled) for value in data]
        if isinstance(data, UUID):
            return str(data)
        if data is None or isinstance(data, (int, float, bool)):
            return data
        raise ValueError("Non-JSON audit value")

    def _redact_schema(self, data: Any) -> Any:
        # A token can be a short word such as "failed". That must not rewrite the
        # schema's outcome enum, IDs, references, hashes, or generated file addresses.
        protected = {
            "id",
            "run_id",
            "capture_id",
            "owner",
            "source_observation",
            "captured_at",
            "recorded_sha256",
            "source_sha256",
            "reference",
            "before_observation",
            "after_observation",
            "started_at",
            "finished_at",
            "created_at",
            "updated_at",
            "at",
            "schema_version",
            "revision",
            "lifecycle",
            "outcome",
            "status",
            "integration",
            "audit_health",
            "role",
            "requirement",
            "applicability",
            "result",
            "type",
            "domain",
            "sha256",
            "retained_sha256",
            "operation",
            "event",
            "primary_failure_id",
            "artifact",
            "fact",
            "head",
            "branch",
            "branch_ref",
            "target_ref",
            "onto_ref",
            "git_directory",
            "common_git_directory",
            "worktree",
            "before_oid",
            "target_oid",
            "onto_oid",
            "result_oid",
        }
        if isinstance(data, dict):
            result: dict[str, Any] = {}
            for key, value in data.items():
                if key == "configuration":
                    result[key] = self._redact(value)
                elif isinstance(value, str) and (key in protected or key.endswith("_artifact")):
                    result[key] = value
                elif (
                    key == "artifacts"
                    and isinstance(value, dict)
                    and all(isinstance(v, str) for v in value.values())
                ):
                    result[key] = dict(value)
                else:
                    result[key] = self._redact_schema(value)
            return result
        if isinstance(data, (list, tuple)):
            return [self._redact_schema(value) for value in data]
        return self._redact(data)

    def initialize(self) -> None:
        with self._lock:
            self._ensure_open()
            try:
                self._store.initialize()
            except Exception as exc:
                raise AuditError("Cannot initialize private audit storage") from exc
            self._state.lifecycle = "active"
            self.save()

    def annotate(self, **values: Any) -> None:
        protected = {
            "status",
            "integrated",
            "integration",
            "result_oid",
            "failure",
            "evidence",
            "audit_errors",
        }
        if protected & values.keys():
            raise ValueError("Execution facts require an explicit audit transition")
        with self._lock:
            self._ensure_open()
            immutable = {
                "branch",
                "before_oid",
                "target",
                "target_oid",
                "target_ref",
                "onto",
                "onto_oid",
                "onto_ref",
                "configuration",
                "prompt",
                "worktree",
                "provider",
                "model",
                "operation",
                "rebase_options",
                "commit_options",
                "merge_ff",
                "interactive",
                "rerere_autoupdate",
            }
            if self._store.owned and immutable & values.keys():
                raise ValueError("Admitted request and pinned inputs cannot be changed")
            encode_json(values)
            self._state.details.update(copy.deepcopy(values))

    def add_record(self, group: str, data: Mapping[str, Any]) -> Record:
        if group not in ("sessions", "conflicts", "reviews", "native_resolutions", "empty_commits"):
            raise ValueError("Unknown audit record scope")
        with self._lock:
            self._ensure_open()
            encode_json(dict(data))
            records = self._state.details.setdefault(group, [])
            values = copy.deepcopy(dict(data))
            values.setdefault("id", uuid4().hex)
            validate_scope(group, values)
            records.append(values)
            return Record(self, group, len(records) - 1)

    def update_record(self, group: str, index: int, data: dict[str, Any]) -> None:
        with self._lock:
            self._ensure_open()
            if "id" in data:
                raise ValueError("Audit record identity cannot change")
            encode_json(data)
            record = self._state.details[group][index]
            validate_scope(group, record | data)
            if (
                group == "sessions"
                and record.get("status") == "resolved"
                and data.get("status") == "failed"
            ):
                raise ValueError("Evidence failure cannot erase resolved session execution")
            record.update(copy.deepcopy(data))

    def record(self, group: str, index: int = -1) -> Record:
        records = self._state.details.get(group, [])
        if not records:
            raise ValueError("Audit record does not exist")
        return Record(self, group, index % len(records))

    def save(self) -> None:
        with self._lock:
            if self._finalized:
                raise ValueError("A finalized audit cannot be changed")
            self._state.revision += 1
            self._store.begin_publication()
            try:
                payload = self._redact_schema(self._state.snapshot())
                content = self._store.snapshot(payload)
            except Exception as exc:
                raise AuditError("Cannot publish audit checkpoint") from exc
            self._published_revision = self._state.revision
            self._published_bytes = content

    def set_status(
        self,
        status: RunStatus,
        error: BaseException | None = None,
        *,
        phase: str = "audit",
        message: str | None = None,
    ) -> None:
        with self._lock:
            if self._finalized:
                return
            if error is not None and self._state.failure is None:
                diagnostic = (
                    self._redact(message)
                    if message is not None
                    else safe_error(error, self._secrets)
                )
                self._state.failure = {
                    "id": uuid4().hex,
                    "domain": "audit" if isinstance(error, AuditError) else "execution",
                    "owner": self.directory.name,
                    "phase": phase,
                    "type": type(error).__name__,
                    "message": diagnostic,
                }
                if isinstance(error, AuditError) and error.artifact is not None:
                    self._state.failure["artifact"] = error.artifact
                self._state.issues.append(self._state.failure)
            if self._state.failure is None or status != "succeeded":
                self._state.status = status

    def completed(self, oid: str) -> None:
        with self._lock:
            self._ensure_open()
            if self._state.integration == "completed" and self._state.result_oid != oid:
                raise EzError("HEAD changed after the observed Git integration result")
            self._state.integration = "completed"
            self._state.result_oid = oid
            self.expect("result.diff", "result_diff")
            if self.view.get("operation") == "rebase":
                self.expect("range-diff.txt", "range_diff")

    def result(self, oid: str) -> None:
        with self._lock:
            self._ensure_open()
            if self._state.result_oid is not None and self._state.result_oid != oid:
                raise EzError("HEAD changed after the observed Git integration result")
            self._state.result_oid = oid

    def checked(self, error: BaseException | None = None) -> None:
        with self._lock:
            self._ensure_open()
            if error is not None:
                self._state.final_checks = (
                    "failed"
                    if isinstance(error, EzError) and not isinstance(error, AuditError)
                    else "unknown"
                )
                self._state.details["final_check_facts"] = {
                    "checked_at": now(),
                    "status": self._state.final_checks,
                    "error_type": type(error).__name__,
                    "reason": safe_error(error, self._secrets),
                }
                return
            self._state.final_checks = "passed"
            self._state.details["final_check_facts"] = {
                "checked_at": now(),
                "result_oid": self._state.result_oid,
                "branch": self.view["branch"],
                "target_oid": self.view["target_oid"],
                "checks": [
                    "HEAD",
                    "clean_checkout",
                    "branch_identity",
                    "pinned_inputs",
                    "ancestry",
                ],
            }

    def record_final_check(
        self, name: str, expected: Any, observed: Any, *, error: BaseException | None = None
    ) -> None:
        with self._lock:
            self._ensure_open()
            record = {
                "id": uuid4().hex,
                "name": name,
                "at": now(),
                "expected": expected,
                "observed": observed,
                "status": "unknown"
                if error is not None
                else ("passed" if expected == observed else "failed"),
            }
            if error is not None:
                record["error"] = safe_error(error, self._secrets)
            encode_json(record)
            self._state.details.setdefault("final_check_records", []).append(record)

    def record_observation(self, git: Git) -> dict[str, Any]:
        with self._lock:
            self._ensure_open()
            value = observe(git)
            with self._lock:
                self._state.observations.append(copy.deepcopy(value))
            return value

    def step_id(self, index: int) -> str:
        return str(self._state.steps[index]["id"])

    def begin_step(self, git: Git, command: tuple[str, ...]) -> int:
        with self._lock:
            self._ensure_open()
            if self._closing:
                raise AuditError("Run finalization has started; refusing another Git mutation")
            if not self.evidence_ready:
                raise AuditError(
                    "Required audit evidence is incomplete; refusing another Git mutation"
                )
            with self._lock:
                previous_integration = self._state.integration
                self._state.steps.append(
                    {
                        "id": uuid4().hex,
                        "argv": self._redact(list(command)),
                        "intent_at": now(),
                        "purpose": "stage" if command[0] == "add" else "integration",
                        "result": "pending",
                        "returncode": None,
                    }
                )
                index = len(self._state.steps) - 1
            try:
                before = self.record_observation(git)
                self._state.steps[index]["before"] = before["id"]
                if before["errors"]:
                    raise AuditError("Cannot observe Git state before mutation")
                self._state.integration = "in_progress"
                self.save()
            except BaseException as exc:
                self._state.steps[index].update(
                    result="not_launched", error_type=type(exc).__name__
                )
                self._state.integration = previous_integration
                raise
            return index

    def returned(self, git: Git, index: int, returncode: int) -> bool:
        # The native result is known before any fallible capture/observation.
        with self._lock:
            self._state.steps[index].update(
                result="returned", returncode=returncode, returned_at=now()
            )
        after = self.record_observation(git)
        with self._lock:
            self._state.steps[index]["after"] = after["id"]
            for channel in ("stdout", "stderr"):
                artifact = f"git-steps/{index + 1:04d}.{channel}"
                self.expect(artifact, f"git_step_{channel}")
                self._state.steps[index][channel + "_artifact"] = artifact
            if (
                self._state.steps[index]["purpose"] == "integration"
                and returncode == 0
                and not after["errors"]
                and after["operation_active"] is False
            ):
                self.completed(after["head"])
            elif self._state.integration != "completed":
                self._state.integration = "unknown" if after["errors"] else "not_completed"
        return not after["errors"]

    def _ensure_open(self) -> None:
        if self._finalized:
            raise ValueError("A finalized audit cannot be changed")

    @property
    def owned(self) -> bool:
        return self._store.owned

    def expect(self, artifact: str, role: str) -> None:
        with self._lock:
            self._ensure_open()
            self._state.evidence.setdefault(
                artifact,
                {
                    "status": "unavailable",
                    "result": "not_attempted",
                    "requirement": "blocking",
                    "role": role,
                },
            )

    def unavailable(self, artifact: str, reason: str, *, optional: bool = False) -> None:
        with self._lock:
            self._ensure_open()
            self._state.evidence[artifact] = {
                "status": "unavailable",
                "result": "not_attempted",
                "reason": reason,
                "requirement": "optional" if optional else "blocking",
            }

    def limit_evidence(self, artifact: str, result: str, reason: str) -> None:
        with self._lock:
            self._ensure_open()
            self._state.evidence[artifact]["status"] = "partial"
            self._state.evidence[artifact]["result"] = result
            self._state.evidence[artifact]["reason"] = reason
            if result == "redacted":
                self._state.evidence[artifact]["redacted"] = True
            elif result == "truncated":
                self._state.evidence[artifact]["truncated"] = True

    def source_size(self, artifact: str, size: int, observation: str | None = None) -> None:
        with self._lock:
            self._ensure_open()
            self._state.evidence[artifact]["source_bytes"] = size
            if observation is not None:
                self._state.evidence[artifact]["source_observation"] = observation

    def _registered(
        self,
        artifact: str,
        *,
        optional: bool = False,
        prefix: bool = False,
        capture_id: str | None = None,
        role: str = "proof",
    ) -> None:
        size, digest = fingerprint(self._store.path(artifact))
        previous: Evidence = self._state.evidence.get(artifact, {"status": "unavailable"})
        self._state.evidence[artifact] = {
            **previous,
            "id": artifact,
            "owner": artifact_owner(artifact, self._state.policy_data()),
            "captured_at": now(),
            "status": (
                "available"
                if previous.get("result") in (None, "not_attempted")
                else previous["status"]
            ),
            "result": "ok"
            if previous.get("result") in (None, "not_attempted")
            else previous["result"],
            "requirement": "optional" if optional else "blocking",
            "retained_bytes": size,
            "sha256": digest,
            "prefix": prefix,
            "capture_id": capture_id or uuid4().hex,
            "role": required_artifacts(self._state.policy_data()).get(
                artifact, previous.get("role", "metadata_export" if optional else role)
            ),
        }

    @property
    def evidence_health(self) -> str:
        return self._state.health()

    @property
    def evidence_ready(self) -> bool:
        return self._state.health() != "incomplete"

    def session_health(self, identifier: str) -> str:
        with self._lock:
            session = next(
                record
                for record in self._state.details.get("sessions", [])
                if record["id"] == identifier
            )
            prefix = session["artifacts"].rstrip("/") + "/"
            evidence = {
                path: entry
                for path, entry in self._state.evidence.items()
                if path.startswith(prefix)
            }
            expected = required_artifacts(self._state.policy_data())
            if any(path.startswith(prefix) and path not in evidence for path in expected):
                return "incomplete"
            issues = [issue for issue in self._state.audit_errors if issue["owner"] == identifier]
            from .audit_state import evidence_health

            return evidence_health(evidence, issues, [])

    def create_directory(self, path: Path) -> None:
        with self._lock:
            self._ensure_open()
            if not self._store.owned:
                raise OSError("Audit directory is not owned by this run")
            relative = path.relative_to(self.directory).as_posix()
            with directory_fd(self._store.path(relative), create=True):
                pass

    def export_json(self, path: Path, data: object) -> None:
        relative = path.relative_to(self.directory).as_posix()
        self.attempt(
            "metadata export",
            lambda: self.write_json(relative, data, optional=True),
            relative,
            optional=True,
        )

    def register_external(self, path: str) -> None:
        with self._lock:
            self._ensure_open()
            self._registered(path)

    def step_unknown(self, index: int, error: BaseException) -> None:
        with self._lock:
            self._ensure_open()
            self._state.steps[index].update(result="unknown", error_type=type(error).__name__)
            if self._state.integration != "completed":
                self._state.integration = "unknown"

    def trace(self, command: tuple[str, ...], output: bytes) -> None:
        with self._lock:
            self._ensure_open()

            def append() -> None:
                if "integration.log" in self._blocked_streams:
                    return
                text = ("git " + " ".join(command) + "\n").encode() + output + b"\n"
                for secret in self._secrets:
                    text = text.replace(secret.encode(), b"<redacted>")
                self._store.append("integration.log", text)
                self._registered("integration.log", optional=True, prefix=True)

            failure = self.attempt("native trace export", append, "integration.log", optional=True)
            if failure is not None:
                self._blocked_streams.add("integration.log")

    def write(
        self,
        artifact: str,
        content: bytes,
        *,
        optional: bool = False,
        capture_id: str | None = None,
        role: str = "proof",
        retained_limit: int | None = None,
    ) -> None:
        with self._lock:
            self._ensure_open()
            existing = self._state.evidence.get(artifact)
            if optional and (
                artifact in required_artifacts(self._state.policy_data())
                or existing is not None
                and existing.get("requirement") == "blocking"
            ):
                raise ValueError("An expected proof cannot be written as an optional export")
            original = content
            content = redact_bytes(content, self._secrets)
            redacted = original != content
            truncated = retained_limit is not None and len(content) > retained_limit
            if retained_limit is not None:
                content = content[:retained_limit]
            try:
                self._store.write(artifact, content)
                self._registered(artifact, optional=optional, capture_id=capture_id, role=role)
                self._state.evidence[artifact]["source_bytes"] = len(original)
            except (OSError, ValueError) as exc:
                self._state.evidence[artifact] = {
                    "status": "unavailable",
                    "result": "io_error",
                    "reason": safe_error(exc, self._secrets),
                    "requirement": "optional" if optional else "blocking",
                    "role": self._state.evidence.get(artifact, {"status": "unavailable"}).get(
                        "role",
                        required_artifacts(self._state.policy_data()).get(
                            artifact, "metadata_export" if optional else role
                        ),
                    ),
                }
                if optional:
                    raise
                raise AuditError("Cannot publish required audit evidence", artifact) from exc
            if redacted:
                self._state.evidence[artifact]["redacted"] = True
                self.limit_evidence(artifact, "redacted", "Known credentials removed from evidence")
            if truncated:
                self._state.evidence[artifact]["truncated"] = True
                self.limit_evidence(
                    artifact, "truncated", "Retained evidence exceeds configured limit"
                )

    def write_json(
        self,
        artifact: str,
        data: object,
        *,
        optional: bool = False,
        schema: bool = False,
        capture_id: str | None = None,
        role: str = "proof",
    ) -> None:
        normalized = self._redact(data, enabled=False)
        transformed = self._redact_schema(normalized) if schema else self._redact(normalized)
        self.write(
            artifact, encode_json(transformed), optional=optional, capture_id=capture_id, role=role
        )
        self.source_size(artifact, len(encode_json(normalized)))
        if transformed != normalized:
            self.limit_evidence(
                artifact, "redacted", "Known credentials removed from JSON evidence"
            )

    def append_event(self, artifact: str, data: object) -> None:
        with self._lock:
            self._ensure_open()
            if artifact in self._blocked_streams:
                raise EzError("Audit event stream has an earlier capture gap")
            normalized = self._redact(data, enabled=False)
            if not isinstance(normalized, dict) or not isinstance(normalized.get("event"), str):
                raise ValueError("An audit event must have a structured event name")
            normalized.setdefault("at", now())
            if isinstance(normalized, dict):
                if "run_id" in normalized:
                    normalized["callback_run_id"] = normalized["run_id"]
                normalized["run_id"] = self._state.run_id
                previous_stream = self._state.evidence.get(artifact)
                normalized["sequence"] = (
                    previous_stream.get("records", 0) if previous_stream is not None else 0
                ) + 1
            transformed = self._redact(normalized)
            if isinstance(normalized, dict) and isinstance(transformed, dict):
                for key in ("at", "event", "run_id"):
                    if key in normalized:
                        transformed[key] = normalized[key]
            content = (json.dumps(transformed, ensure_ascii=True, allow_nan=False) + "\n").encode()
            digest = self._stream_hashes.get(artifact)
            digest = hashlib.sha256() if digest is None else digest.copy()
            previous = self._state.evidence.get(artifact, {"status": "available"})
            self._store.append(artifact, content)
            digest.update(content)
            self._stream_hashes[artifact] = digest
            self._state.evidence[artifact] = {
                **previous,
                "status": previous.get("status", "available"),
                "result": previous.get("result", "ok"),
                "role": "event_stream",
                "id": artifact,
                "owner": artifact_owner(artifact, self._state.policy_data()),
                "captured_at": now(),
                "records": previous.get("records", 0) + 1,
                "requirement": "blocking",
                "retained_bytes": previous.get("retained_bytes", 0) + len(content),
                "sha256": digest.hexdigest(),
                "prefix": True,
                "capture_id": previous.get("capture_id", uuid4().hex),
            }
            if transformed != normalized:
                self.limit_evidence(
                    artifact, "redacted", "Known credentials removed from event content"
                )

    def capture_error(
        self,
        phase: str,
        error: Exception,
        artifact: str | None,
        *,
        optional: bool = False,
        owner: str | None = None,
    ) -> None:
        with self._lock:
            self._ensure_open()
            underlying = (
                error.__cause__ if isinstance(error, AuditError) and error.__cause__ else error
            )
            failure: Failure = {
                "phase": phase,
                "type": type(underlying).__name__,
                "message": safe_error(error, self._secrets),
            }
            if artifact is not None:
                failure["artifact"] = artifact
                previous: Evidence = self._state.evidence.get(artifact, {"status": "unavailable"})
                self._state.evidence[artifact] = {
                    **previous,
                    "status": "partial" if previous.get("sha256") else "unavailable",
                    "result": previous["result"]
                    if previous.get("result") in ("observation_error", "invalid", "interrupted")
                    else "io_error",
                    "reason": failure["message"],
                    "requirement": "optional" if optional else "blocking",
                    "role": required_artifacts(self._state.policy_data()).get(
                        artifact, previous.get("role", "metadata_export" if optional else "proof")
                    ),
                }
            failure["id"] = uuid4().hex
            failure["domain"] = "audit"
            failure["owner"] = owner or (
                artifact_owner(artifact, self._state.policy_data())
                if artifact
                else self.directory.name
            )
            failure["blocking"] = not optional
            self._state.audit_errors.append(failure)
            self._state.issues.append(failure)
            if optional:
                self.presentation_errors.append(f"{phase}: {failure['message']}")

    def attempt(
        self,
        phase: str,
        action: Callable[[], object],
        artifact: str | None = None,
        *,
        append: bool = False,
        optional: bool = False,
        owner: str | None = None,
    ) -> Exception | None:
        try:
            action()
            if artifact is not None and artifact not in self._state.evidence:
                self._registered(artifact, optional=optional, prefix=append)
        except Exception as exc:
            self.capture_error(phase, exc, artifact, optional=optional, owner=owner)
            if append and artifact is not None:
                self._blocked_streams.add(artifact)
            return exc
        return None

    def has_artifact(self, artifact: str) -> bool:
        entry = self.evidence.get(artifact)
        if entry is None or "sha256" not in entry:
            return False
        try:
            size, digest = fingerprint(
                self._store.path(artifact),
                entry.get("retained_bytes") if entry.get("prefix") else None,
            )
            return (size, digest) == (entry.get("retained_bytes"), entry["sha256"])
        except (OSError, ValueError):
            return False

    def artifact(self, number: int, filename: str, content: str) -> str:
        path = f"conflicts/{number:04d}/{filename}"
        self.write(path, content.encode("utf-8"))
        return path

    def capture_worktree(
        self,
        git: Git,
        directory: Path,
        limit: int,
        paths: list[str] | None = None,
        reference: str = "HEAD",
    ) -> None:
        self._ensure_open()
        capture_worktree(self, git, directory, limit, paths, reference)

    def seal_directory(self, directory: Path) -> None:
        # SDK-owned files are indexed only after its writers have settled. Walk
        # without following directory links; regular_fd rejects unsafe file entries.
        with self._lock:
            self._ensure_open()

            def failed(error: OSError) -> None:
                raise error

            with directory_fd(directory):
                pass
            for parent, directories, files in os.walk(directory, followlinks=False, onerror=failed):
                for name in [*directories, *files]:
                    path = Path(parent) / name
                    if path.is_symlink():
                        raise EzError("Unsafe agent file in audit directory")
                for name in files:
                    relative = (Path(parent) / name).relative_to(self.directory).as_posix()
                    self._seal_file(relative)

    def _seal_file(self, relative: str) -> None:
        if relative in self._state.evidence:
            raise AuditError("Agent file was already sealed", relative)
        digest = hashlib.sha256()
        with regular_fd(self._store.path(relative)) as descriptor:
            before = os.fstat(descriptor)

            def blocks() -> Iterator[bytes]:
                while block := os.read(descriptor, 65536):
                    digest.update(block)
                    yield block
                after = os.fstat(descriptor)
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise AuditError("Agent file changed while being sealed", relative)

            self._store.write(
                relative,
                redact_chunks(blocks(), tuple(secret.encode() for secret in self._secrets)),
                replace=True,
            )
        self._registered(relative)
        self._state.evidence[relative]["source_bytes"] = before.st_size
        if digest.hexdigest() != self._state.evidence[relative]["sha256"]:
            self.limit_evidence(relative, "redacted", "Known credentials removed")

    def begin_finalization(self) -> None:
        with self._lock:
            self._ensure_open()
            self._closing = True
            self._state.lifecycle = "finalizing"

    def finalize(self) -> None:
        with self._lock:
            if self._finalized:
                return
            for group in ("sessions", "conflicts", "reviews"):
                for record in self._state.details.get(group, []):
                    if record.get("status") in ("created", "running", "pending"):
                        record.update(
                            status="unknown",
                            termination_reason="No terminal scope outcome observed",
                        )
            self.begin_finalization()
            self._state.lifecycle = "finished"
            if self._state.health() == "incomplete" and self._state.status in (
                "running",
                "succeeded",
            ):
                self.set_status(
                    "failed",
                    AuditError("Required audit evidence is incomplete; inspect audit errors"),
                )
            if self._state.status == "running":
                self.set_status("failed", EzError("Execution ended without a terminal outcome"))
            failure = self.attempt("manifest", self.save)
            attempted_revision = self._state.revision
            if failure is not None:
                self.set_status(
                    "interrupted" if self._state.status == "interrupted" else "failed",
                    AuditError(
                        "Terminal audit publication failed; last recorded state may be older"
                    ),
                )
                self._state.revision += 1
            publication: Literal["published", "failed", "unknown"] = (
                "published"
                if failure is None
                else "unknown"
                if self._store.replacement_started
                else "failed"
            )
            self._result = AuditResult(
                encode_json(self._redact_schema(self._state.view())),
                publication,
                attempted_revision,
                self._published_revision,
            )
            self._finalized = True
        if failure is None:
            try:
                self.report()
            except Exception as exc:
                self.presentation_errors.append(
                    "Report unavailable: " + safe_error(exc, self._secrets)
                )

    def report(self) -> None:
        if self._published_bytes is None:
            return
        from .audit_state import snapshot_view

        payload = json.loads(self._published_bytes)
        try:
            body = render_markdown(snapshot_view(payload))
            header = {
                "schema": 2,
                "run_id": payload["run_id"],
                "revision": payload["revision"],
                "manifest_sha256": hashlib.sha256(self._published_bytes).hexdigest(),
                "renderer": 1,
                "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            }
            content = (
                "<!-- git-ez-cache "
                + json.dumps(header)
                + " -->\n"
                + (
                    "> Derived report. Use `git ez show` to verify "
                    "the current manifest and evidence.\n\n"
                )
                + body
            )
            self._store.write("report.md", content.encode(), replace=True)
        except Exception as exc:
            self.presentation_errors.append("Report unavailable: " + safe_error(exc, self._secrets))
