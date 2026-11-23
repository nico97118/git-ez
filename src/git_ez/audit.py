# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""A small journal of git-ez interventions, conflict proposals and decisions."""

import copy
import json
import os
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Literal, NotRequired, TypedDict
from uuid import UUID, uuid4

from .audit_report import render_markdown
from .audit_safety import redact_bytes, redact_chunks
from .audit_storage import directory_fd, encode_json, publish, read_regular, write_json
from .errors import AuditError, EzError


class Validation(TypedDict):
    command: list[str]
    status: Literal[
        "not_requested", "not_run", "running", "passed", "failed", "timed_out", "interrupted"
    ]
    returncode: NotRequired[int]


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


def now() -> str:
    return datetime.now(UTC).isoformat()


def safe_error(error: BaseException, secrets: tuple[str, ...] = ()) -> str:
    message = (
        str(error) if isinstance(error, EzError) else f"{type(error).__name__}: operation failed"
    )
    if isinstance(error, KeyboardInterrupt):
        message = "Interrupted by user"
    for secret in secrets:
        if secret:
            message = message.replace(secret, "<redacted>")
    return message


class Record(Mapping[str, Any]):
    def __init__(self, audit: "Audit", data: dict[str, Any]):
        self.audit, self.data = audit, data

    def __getitem__(self, key: str) -> Any:
        return copy.deepcopy(self.data[key])

    def __iter__(self) -> Iterator[str]:
        return iter(self.data)

    def __len__(self) -> int:
        return len(self.data)

    def update(self, *args: Mapping[str, Any], **values: Any) -> None:
        with self.audit._lock:
            self.data.update(*args, **values)

    def artifact(self, label: str, path: str) -> None:
        self.data.setdefault("artifacts", {})[label] = path


class Audit:
    def __init__(self, directory: Path, metadata: dict[str, Any]):
        self.directory = directory
        self._data = copy.deepcopy(metadata)
        self._data.update(id=directory.name, schema_version=4, status="running", integrated=False)
        self._lock = RLock()
        self._secrets: tuple[str, ...] = ()
        self.owned = self.published = self._finished = False
        self.audit_errors: list[dict[str, Any]] = []
        self.presentation_errors: list[str] = []

    @property
    def view(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    @property
    def inputs(self) -> PinnedInputs:
        data = self._data
        return PinnedInputs(
            data["branch"],
            data["before_oid"],
            PinnedRef(data["target_oid"], data.get("target_ref")),
            PinnedRef(data["onto_oid"], data.get("onto_ref")) if data.get("onto_oid") else None,
        )

    @property
    def operation(self) -> str:
        return str(self._data["operation"])

    def protect(self, *secrets: str) -> None:
        self._secrets = tuple(secret for secret in secrets if secret)

    def _clean(self, value: Any, *, content_keys: bool = False) -> Any:
        if isinstance(value, str):
            return redact_bytes(value.encode("utf-8"), self._secrets).decode("utf-8")
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                name = self._clean(key) if content_keys else key
                if name in result:
                    raise ValueError("Credential filtering would merge content keys")
                result[name] = (
                    item
                    if not content_keys
                    and key in ("status", "action", "application", "schema_version", "integrated")
                    else self._clean(item, content_keys=content_keys)
                )
            return result
        if isinstance(value, (list, tuple)):
            return [self._clean(item, content_keys=content_keys) for item in value]
        if value is None or isinstance(value, (int, float, bool)):
            return value
        raise ValueError("Unsupported journal value")

    def initialize(self) -> None:
        with directory_fd(self.directory.parent, create=True) as parent:
            os.mkdir(self.directory.name, mode=0o700, dir_fd=parent)
        self.owned = True
        self.save()

    def annotate(self, **values: Any) -> None:
        self._data.update(values)

    def add_record(self, group: str, data: Mapping[str, Any]) -> Record:
        record = dict(data)
        record.setdefault("id", uuid4().hex)
        self._data.setdefault(group, []).append(record)
        return Record(self, record)

    def record(self, group: str, index: int = -1) -> Record:
        return Record(self, self._data[group][index])

    def save(self) -> None:
        if not self.owned:
            raise AuditError("Journal directory was not created by this run")
        with self._lock:
            self._data["updated_at"] = now()
            write_json(self.directory / "run.json", self._clean(self._data))

    def set_status(
        self,
        status: str,
        error: BaseException | None = None,
        *,
        phase: str = "audit",
        message: str | None = None,
    ) -> None:
        self._data["status"] = status
        if error is not None and "failure" not in self._data:
            diagnostic = self._clean(message) if message else safe_error(error, self._secrets)
            self._data.update(
                error=diagnostic,
                failure={"phase": phase, "type": type(error).__name__, "message": diagnostic},
            )

    def completed(self, oid: str) -> None:
        self._data.update(integrated=True, result_oid=oid)

    def create_directory(self, path: Path) -> None:
        if not self.owned:
            raise AuditError("Journal directory was not created by this run")
        path.relative_to(self.directory)
        with directory_fd(path, create=True):
            pass

    def write(self, artifact: str, content: bytes) -> None:
        path = Path(artifact)
        if not self.owned or path.is_absolute() or ".." in path.parts:
            raise AuditError("Invalid journal destination")
        try:
            publish(self.directory / path, redact_bytes(content, self._secrets), replace=True)
        except (OSError, ValueError) as exc:
            raise AuditError("Cannot retain conflict resolution", artifact) from exc

    def write_json(self, artifact: str, data: object) -> None:
        self.write(artifact, encode_json(self._clean(data)))

    def artifact(self, number: int, filename: str, content: str) -> str:
        path = f"conflicts/{number:04d}/{filename}"
        self.write(path, content.encode("utf-8"))
        return path

    def has_artifact(self, artifact: str) -> bool:
        return (self.directory / artifact).is_file()

    def attempt(
        self, phase: str, action: Callable[[], object], artifact: str | None = None
    ) -> Exception | None:
        try:
            action()
        except Exception as exc:
            self.audit_errors.append(
                {
                    "phase": phase,
                    "type": type(exc).__name__,
                    "message": safe_error(exc, self._secrets),
                    "artifact": artifact,
                }
            )
            return exc
        return None

    def export_json(self, path: Path, data: object) -> None:
        relative = path.relative_to(self.directory).as_posix()
        self.attempt("session metadata", lambda: self.write_json(relative, data), relative)

    def append_event(self, artifact: str, data: object) -> None:
        if not isinstance(data, dict):
            raise ValueError("Expected an event object")
        content = {
            key: value if key in ("event", "at") else self._clean(value, content_keys=True)
            for key, value in data.items()
        }
        path = self.directory / artifact
        with self._lock, directory_fd(path.parent, create=True) as parent:
            descriptor = os.open(
                path.name,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            with os.fdopen(descriptor, "ab") as stream:
                stream.write(
                    (json.dumps(content, ensure_ascii=True, allow_nan=False) + "\n").encode()
                )

    def redact_agent_files(self, directory: Path) -> None:
        for path in directory.rglob("*"):
            if path.is_file():
                original = read_regular(path)
                filtered = b"".join(
                    redact_chunks((original,), tuple(s.encode() for s in self._secrets))
                )
                if filtered != original:
                    publish(path, filtered, replace=True)

    def finalize(self) -> None:
        if self._finished:
            return
        self._finished = True
        if self._data["status"] == "running":
            self.set_status("failed", EzError("Run ended without a recorded result"))
        self._data["finished_at"] = now()
        self._data["audit_errors"] = self.audit_errors
        failure = self.attempt("journal", self.save)
        self.published = failure is None
        if failure is not None:
            if "failure" not in self._data:
                self.set_status("failed", AuditError("Cannot save the resolution journal"))
            return
        error = self.attempt(
            "report",
            lambda: self.write(
                "report.md", render_markdown(self._clean(self._data), self.directory).encode()
            ),
        )
        if error is not None:
            self.presentation_errors.append("Report unavailable; read run.json")


def load_report(directory: Path, *, verbose: bool = False) -> str:
    try:
        data = json.loads(read_regular(directory / "run.json"))
        if (
            not isinstance(data, dict)
            or data.get("schema_version") != 4
            or not isinstance(data.get("conflicts"), list)
        ):
            raise ValueError("Unsupported resolution journal")
        return render_markdown(data, directory, verbose=verbose)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise EzError("Cannot read resolution journal; inspect run.json directly") from exc
