# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Present recorded conflict decisions without recapturing or certifying Git state."""

import re
from typing import Any


def safe_text(value: str) -> str:
    return "".join(c if c.isprintable() and c != "`" else f"\\u{ord(c):04x}" for c in value)


def literal(value: object) -> str:
    return re.sub(r"([\\*_{\[\]()#!<>|])", r"\\\1", safe_text(str(value)))


def render_markdown(data: dict[str, Any]) -> str:
    lines = [
        "# git-ez — resolution journal",
        "",
        f"Run: `{literal(data['id'])}`",
        f"Recorded status: **{literal(data['status'])}**",
        f"Operation: `{literal(data['operation'])}`",
        f"Branch: `{literal(data['branch'])}`",
        f"Integration completed: `{data['integrated']}`",
        "",
        "This journal records git-ez interventions. "
        "Inspect Git for commits and repository history.",
        "Confidence is reported by the model; it is not proof of correctness.",
        "",
    ]
    if data.get("error"):
        lines += [f"Run stopped: {literal(data['error'])}", ""]
    for conflict in data["conflicts"]:
        lines += [
            f"## Conflict {conflict['number']}: {literal(conflict['path'])}",
            "",
            f"Status: `{literal(conflict['status'])}`",
        ]
        resolution = conflict.get("resolution")
        if resolution:
            lines += [
                f"Action: `{literal(resolution['action'])}`",
                f"Confidence: {resolution.get('confidence', 'unavailable')}",
                "",
                literal(resolution.get("rationale", "No explanation was returned.")),
            ]
        else:
            lines += ["No resolution decision was returned."]
        for label, path in conflict.get("artifacts", {}).items():
            lines += [f"- `{literal(label)}`: `{literal(path)}`"]
        if conflict.get("capture_error"):
            lines += [f"Capture unavailable: {literal(conflict['capture_error'])}"]
        lines.append("")
    for review in data.get("reviews", []):
        lines += [
            f"Proposal {review['attempt']}: {literal(review.get('decision', review['status']))}"
        ]
    for error in data.get("audit_errors", []):
        lines += [f"Journal warning ({literal(error['phase'])}): {literal(error['message'])}"]
    if data.get("validation"):
        lines += ["", f"Validation: `{literal(data['validation']['status'])}`"]
    return "\n".join(lines) + "\n"
