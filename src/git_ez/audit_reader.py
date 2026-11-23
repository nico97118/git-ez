# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Read-only verification of authoritative snapshots."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from typing import Any

from .audit_policy import required_artifacts
from .audit_state import AuditView, snapshot_view, validate_snapshot
from .audit_storage import AuditStore, fingerprint, inventory_paths, read_regular, regular_fd
from .errors import EzError


def _stream_fingerprint(path: Path, entry: dict[str, Any], run_id: str) -> tuple[int, str]:
    remaining = entry["retained_bytes"]
    digest = hashlib.sha256()
    pending = b""
    count = 0
    with regular_fd(path) as descriptor:
        while remaining:
            block = os.read(descriptor, min(65536, remaining))
            if not block:
                raise ValueError("Event stream is shorter than its committed prefix")
            digest.update(block)
            remaining -= len(block)
            pending += block
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                record = json.loads(line)
                count += 1
                if (
                    not isinstance(record, dict)
                    or record.get("sequence") != count
                    or record.get("run_id") != run_id
                ):
                    raise ValueError("Invalid event identity or ordering")
    if pending or count != entry.get("records"):
        raise ValueError("Event stream is not an acknowledged whole-record prefix")
    return entry["retained_bytes"], digest.hexdigest()


def _inventory(content: bytes, artifact: str, payload: dict[str, Any]) -> None:
    data = json.loads(content)
    entry = payload["artifacts"][artifact]
    if (
        not isinstance(data, dict)
        or data.get("capture_id") != entry["capture_id"]
        or data.get("capture_errors")
        or not isinstance(data.get("files"), list)
    ):
        raise ValueError("Invalid or incomplete file inventory")
    for record in data["files"]:
        if not isinstance(record, dict) or record.get("state") not in ("file", "deleted"):
            raise ValueError("File inventory has unavailable contents")
        if record["state"] == "deleted":
            continue
        relative = record.get("artifact")
        if not isinstance(relative, str):
            raise ValueError("Missing file capture reference")
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Unsafe file inventory reference")
        child = payload["artifacts"].get(str(PurePosixPath(artifact).parent / path))
        if child is None or (
            child.get("requirement") != "blocking"
            or child.get("capture_id") != entry["capture_id"]
            or child.get("sha256") != record.get("recorded_sha256")
            or child.get("retained_bytes") != record.get("recorded_bytes")
            or child.get("source_bytes") != record.get("size")
        ):
            raise ValueError("File capture is missing or contradicts its inventory")


def load_audit(directory: Path) -> AuditView:
    try:
        content = read_regular(directory / "run.json")
        payload: Any = json.loads(content)
        if not isinstance(payload, dict):
            raise ValueError("Audit manifest must be an object")
        validate_snapshot(payload)
        issues: list[str] = []
        unverified = payload["lifecycle"] != "finished" or payload["audit_health"] == "incomplete"
        if payload["lifecycle"] != "finished":
            issues.append(
                "Last recorded state is not finalized; this does not establish process liveness."
            )
        store = AuditStore(directory)
        for artifact in required_artifacts(snapshot_view(payload)):
            if artifact not in payload["artifacts"]:
                issues.append(f"Required evidence was not captured: {artifact}")
                unverified = True
        for artifact, entry in payload["artifacts"].items():
            path = store.path(artifact)
            if entry["status"] == "unavailable" or "sha256" not in entry:
                if entry.get("requirement", "blocking") == "blocking":
                    issues.append(f"Required evidence unavailable: {artifact}")
                    unverified = True
                elif entry.get("result") != "not_attempted":
                    issues.append(f"Optional evidence unavailable: {artifact}")
                continue
            try:
                inventory = None
                if entry.get("role") in ("worktree_inventory", "final_inventory"):
                    inventory = read_regular(path)
                    size, digest = len(inventory), hashlib.sha256(inventory).hexdigest()
                elif entry.get("role") == "event_stream":
                    size, digest = _stream_fingerprint(path, entry, payload["run_id"])
                else:
                    size, digest = fingerprint(
                        path, entry.get("retained_bytes") if entry.get("prefix") else None
                    )
                if (size, digest) != (entry["retained_bytes"], entry["sha256"]):
                    raise ValueError("Retained evidence does not match its manifest")
                if entry.get("result") in (
                    "io_error",
                    "observation_error",
                    "invalid",
                    "interrupted",
                ):
                    raise ValueError("Capture did not satisfy its evidence requirement")
                if inventory is not None:
                    _inventory(inventory, artifact, payload)
                if entry.get("prefix"):
                    with regular_fd(path) as descriptor:
                        if os.fstat(descriptor).st_size > size:
                            issues.append(f"Unindexed stream tail, not verified: {artifact}")
            except (OSError, ValueError) as exc:
                if entry.get("requirement", "blocking") == "blocking":
                    issues.append(f"Unverifiable evidence: {artifact} ({type(exc).__name__})")
                    unverified = True
                else:
                    issues.append(
                        f"Optional evidence unverifiable: {artifact} ({type(exc).__name__})"
                    )
        if payload["audit_health"] == "incomplete":
            issues.append("The recorded audit has incomplete required evidence.")
        try:
            for raw_path in inventory_paths(directory):
                if raw_path not in payload["artifacts"] and raw_path not in (
                    "run.json",
                    "report.md",
                ):
                    issues.append(f"Unindexed raw artifact, not verified: {raw_path}")
        except OSError:
            issues.append("Cannot enumerate unindexed raw artifacts")
        from .audit_report import safe_text

        return AuditView(content, tuple(map(safe_text, issues)), 1 if unverified else 0)
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        raise EzError(
            f"Cannot verify audit manifest ({type(exc).__name__}); "
            "inspect raw files without trusting cached reports"
        ) from exc


def minimal_text(view: AuditView) -> str:
    from .audit_report import safe_text

    data = view.data
    return "\n".join(
        [
            f"Run: {safe_text(str(data.get('id', 'unknown')))}",
            f"Recorded status: {safe_text(str(data.get('status', 'unknown')))}",
            f"Git integration: {safe_text(str(data.get('integration', 'unverified')))}",
            f"Evidence: {safe_text(str(data.get('audit_health', 'unverified')))}",
            *view.diagnostics,
        ]
    )
