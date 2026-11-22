# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Read-only verification of authoritative snapshots, including conservative legacy views."""

import json
from pathlib import Path
from typing import Any

from .audit_state import AuditView, validate_snapshot
from .audit_storage import AuditStore, fingerprint, read_regular
from .errors import EzError


def load_audit(directory: Path) -> AuditView:
    try:
        content = read_regular(directory / "run.json")
        payload: Any = json.loads(content)
        if not isinstance(payload, dict):
            raise ValueError("Audit manifest must be an object")
        if payload.get("schema_version", 1) == 1:
            return AuditView(
                content,
                ("Unverified legacy audit; recorded state may be incomplete or stale.",),
                1,
                True,
            )
        validate_snapshot(payload)
        issues: list[str] = []
        if payload["lifecycle"] != "finished":
            issues.append(
                "Last recorded state is not finalized; this does not establish process liveness."
            )
        store = AuditStore(directory)
        for artifact, entry in payload["artifacts"].items():
            path = store.path(artifact)
            if entry["status"] == "unavailable" or "sha256" not in entry:
                if entry.get("requirement", "blocking") == "blocking":
                    issues.append(f"Required evidence unavailable: {artifact}")
                continue
            try:
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
            except (OSError, ValueError) as exc:
                if entry.get("requirement", "blocking") == "blocking":
                    issues.append(f"Unverifiable evidence: {artifact} ({type(exc).__name__})")
        if payload["audit_health"] == "incomplete":
            issues.append("The recorded audit has incomplete required evidence.")
        return AuditView(content, tuple(issues), 1 if issues else 0)
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
