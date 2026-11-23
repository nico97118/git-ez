# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Read saved interventions and present them without reconstructing Git history."""

import json
import re
from pathlib import Path
from typing import Any

from .audit_storage import read_regular


def safe_text(value: str) -> str:
    return "".join(c if c.isprintable() and c != "`" else f"\\u{ord(c):04x}" for c in value)


def literal(value: object) -> str:
    return re.sub(r"([\\*_{\[\]()#!<>|])", r"\\\1", safe_text(str(value)))


def saved_text(directory: Path, relative: str) -> str:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Unsafe journal path")
    return read_regular(directory / path).decode("utf-8")


def artifact_lines(directory: Path | None, relative: str | None) -> list[str]:
    if not relative:
        return ["Capture unavailable."]
    if directory is None:
        return [f"File: {literal(relative)}"]
    try:
        content = saved_text(directory, relative)
        if not content:
            return ["(no changes)"]
        return [safe_text(line) for line in content.removesuffix("\n").split("\n")]
    except (OSError, ValueError):
        return [f"Capture unavailable: {literal(relative)}"]


def render_markdown(
    data: dict[str, Any], directory: Path | None = None, *, verbose: bool = False
) -> str:
    lines = [
        "# git-ez — resolution journal",
        "",
        f"Run: {literal(data['id'])}",
        f"Recorded status: **{literal(data['status'])}**",
        f"Operation: {literal(data['operation'])} {literal(data.get('target', ''))}",
        f"Branch: {literal(data['branch'])}",
        f"Checkout: {literal(data.get('worktree', 'unavailable'))}",
        f"Started: {literal(data.get('created_at', 'unavailable'))}",
        f"Finished: {literal(data.get('finished_at', 'unavailable'))}",
        f"Integration completed: {data['integrated']}",
        f"Conflicts: {len(data['conflicts'])}",
        "",
        "This journal records git-ez interventions. Inspect Git for repository history.",
        "Confidence is reported by the model; it is not proof of correctness.",
        "",
    ]
    if data["status"] == "running":
        lines += ["Unfinished record; this does not establish process liveness.", ""]
    if data.get("error"):
        lines += [f"Run stopped: {literal(data['error'])}", ""]
    for conflict in data["conflicts"]:
        lines += [
            f"## Conflict {conflict['number']}: {literal(conflict['path'])}",
            "",
            "Conflict diff (ours -> theirs):",
        ]
        lines += artifact_lines(directory, conflict.get("artifacts", {}).get("conflict_diff"))
        attempts = [
            r for r in data.get("reviews", []) if conflict["id"] in r.get("conflict_ids", [])
        ]
        for attempt in attempts:
            disposition = literal(attempt.get("decision", attempt["status"]))
            lines += [
                "",
                f"Attempt {attempt['attempt']}: {disposition}",
                f"Application: {literal(attempt.get('application', 'not_staged'))}",
            ]
            resolution = attempt.get("resolution") or {}
            lines += [
                f"Confidence: {resolution.get('confidence', 'unavailable')}",
                literal(resolution.get("rationale", "No explanation was returned.")),
            ]
            if len(attempt.get("conflict_ids", [])) > 1:
                lines += ["Explanation and confidence are shared by this conflict group."]
            if attempt.get("feedback"):
                lines += [f"Feedback sent to model: {literal(attempt['feedback'])}"]
            if attempt.get("user_feedback"):
                lines += [f"User feedback: {literal(attempt['user_feedback'])}"]
            proposal: dict[str, Any] = next(
                (c for c in attempt.get("conflicts", []) if c["number"] == conflict["number"]), {}
            )
            action = proposal.get("resolution", {}).get("action", "unavailable")
            lines += [f"File action: {literal(action)}", "Resolution diff (ours -> proposal):"]
            lines += artifact_lines(directory, proposal.get("artifacts", {}).get("diff"))
            if attempt.get("error"):
                lines += [f"Attempt stopped: {literal(attempt['error'])}"]
        if not attempts:
            lines += ["No resolution attempt was recorded."]
        if conflict.get("capture_error"):
            lines += [f"Capture unavailable: {literal(conflict['capture_error'])}"]
        lines.append("")
    for error in data.get("audit_errors", []):
        lines += [f"Partial journal ({literal(error['phase'])}): {literal(error['message'])}"]
    validation = data.get("validation")
    if validation:
        lines += [
            "",
            f"Validation: {literal(validation['status'])}",
            f"Command: {literal(json.dumps(validation['command']))}",
        ]
        if "returncode" in validation:
            lines += [f"Exit code: {validation['returncode']}"]
    if verbose and directory is not None:
        for session in data.get("sessions", []):
            lines += [
                "",
                f"## Model/tool events — attempt {session['round']}",
                f"Provider/model: {literal(data.get('provider'))}/{literal(data.get('model'))}",
            ]
            relative = f"{session['artifacts']}/events.jsonl"
            try:
                for line in saved_text(directory, relative).splitlines():
                    try:
                        event = json.loads(line)
                        lines += [
                            safe_text(part)
                            for part in json.dumps(event, indent=2, ensure_ascii=True).splitlines()
                        ]
                    except ValueError:
                        lines += ["Incomplete or malformed event:", safe_text(line)]
            except (OSError, ValueError):
                lines += [f"Events unavailable: {literal(relative)}"]
        if validation and validation["status"] not in ("not_run", "not_requested"):
            for stream in ("stdout", "stderr"):
                lines += ["", f"Validation {stream}:"] + artifact_lines(
                    directory, f"validation.{stream}"
                )
    if directory is not None:
        lines += ["", f"Files: {literal(directory)}"]
    return "\n".join(lines) + "\n"
