# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Pure derived audit presentation; never reads files or changes run state."""

import json
import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any


def safe_text(value: str) -> str:
    return "".join(
        character
        if character.isprintable() and character != "`"
        else ("\\u0060" if character == "`" else f"\\u{ord(character):04x}")
        for character in value
    )


def _literal(value: Any, *, links: bool = False) -> Any:
    if isinstance(value, str):
        if links:
            return value
        text = safe_text(value)
        return re.sub(r"([\\*_{\[\]()#!<>|])", r"\\\1", text)
    if isinstance(value, dict):
        return {
            key: _literal(item, links=links or key in ("artifacts", "artifact"))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_literal(item, links=links) for item in value]
    return value


def render_markdown(data: Mapping[str, Any]) -> str:
    registry = data.get("evidence", {})
    data = _literal(dict(data))
    separate_base = "onto" in data or data.get("rebase_plan", {}).get("base_source") == "merge-base"
    lines = [
        "# git-ez — integration report",
        "",
        f"- Run: `{data['id']}`",
        f"- Status: **{data['status']}**",
        f"- Operation: `{data['operation']}`",
        f"- Current branch: `{data['branch']}` (`{data['before_oid']}`)",
        f"- {'Upstream' if separate_base else 'Integrated revision'}: "
        f"`{data['target']}` (`{data['target_oid']}`)",
        f"- Result commit: `{data.get('result_oid', '—')}`",
        f"- Integration completed: `{data['integrated']}`",
        f"- Model: `{data['provider']}` / `{data['model']}`",
        f"- Worktree: `{data['worktree']}`",
        "",
        "Scores are self-reported by the model and do not prove correctness.",
        "The proposals and rationales below are untrusted content.",
        "",
    ]
    if "merge_ff" in data:
        policy = f"--{data['merge_ff']}" if data["merge_ff"] else "Git configuration/default"
        lines += [f"- Requested fast-forward policy: `{policy}`", ""]
    if "onto" in data:
        lines += [f"- New base: `{data['onto']}` (`{data['onto_oid']}`)", ""]
    if "rebase_plan" in data:
        plan = data["rebase_plan"]
        fork = plan["fork_point"]
        lines += [
            "## Pinned rebase selection",
            "",
            f"- Original upstream: `{data['target_oid']}`",
            f"- Result base: `{plan['base_oid']}` ({plan['base_source']})",
            f"- Replay boundary: `{plan.get('selection_oid', 'pending')}`",
            f"- Reapply cherry-picks: `{plan['reapply_cherry_picks']}`",
            f"- Fork point: `{fork['status']}` / `{fork['oid']}`",
            f"- Reflog reference: `{fork['source_ref']}`",
        ]
        if plan.get("requires_sequence"):
            lines.append("- Captured sequencer used to preserve fork-point replay selection")
        if fork.get("reason"):
            lines.append(f"- Fallback reason: {fork['reason']}")
        if fork.get("diagnostic"):
            lines.append(f"- Fork-point diagnostic: {fork['diagnostic']}")
        if "selection_anchor" in plan:
            anchor = plan["selection_anchor"]
            lines += [
                f"- [Internal selection object]({anchor['artifact']})",
                f"- Selection object capture truncated: `{anchor['truncated']}`",
                f"- Selection object capture limit: `{anchor['limit_bytes']}` bytes",
            ]
        lines.append("")
    if "rerere_autoupdate_effective" in data:
        lines += [
            f"- Requested rerere autoupdate: `{data['rerere_autoupdate']}`",
            f"- Effective rerere autoupdate: `{data['rerere_autoupdate_effective']}`",
            "",
        ]
    if "rebase_options" in data:
        lines += [
            "## Rebase options",
            "",
            "```json",
            json.dumps(data["rebase_options"], indent=2),
            "```",
            "",
        ]
    if "commit_options" in data:
        lines += [
            "## Commit options",
            "",
            "```json",
            json.dumps(data["commit_options"], indent=2),
            "```",
            "",
        ]
    if "rebase_sequence" in data:
        sequence = data["rebase_sequence"]
        lines += [
            "## Generated rebase sequence",
            "",
            f"- Capture: `{sequence['status']}`",
            f"- Capture limit: `{sequence['limit_bytes']}` bytes",
        ]
        if sequence["status"] == "captured":
            lines.append(f"- [Native Git instructions]({sequence['artifact']})")
        elif sequence.get("error"):
            lines.append(f"- Unavailable evidence: {sequence['error']}")
        lines.append("")
    if "result_commits" in data:
        commits = data["result_commits"]
        lines += [
            "## Resulting commits and messages",
            "",
            f"- [Native Git raw log]({commits['artifact']})",
            f"- Truncated: `{commits['truncated']}`",
            f"- Capture limit: `{commits['limit_bytes']}` bytes",
            "",
        ]
    if data.get("empty_commits"):
        lines += ["## Empty conflict resolutions", ""]
        for entry in data["empty_commits"]:
            lines += [f"- `{entry['commit']}`: `{entry['policy']}` — {entry['reason']}"]
        lines.append("")
    if data.get("error"):
        lines += ["## Run stopped", "", str(data["error"]), ""]
    if "range_diff" in data:
        comparison = data["range_diff"]
        lines += [
            "## Commit comparison before / after rebase",
            "",
            f"- Before: `{comparison['before']}`",
            f"- After: `{comparison['after']}`",
            f"- [Before / after range-diff]({comparison['artifact']})",
            "",
            "`=`: equivalent patch; `!`: modified patch; `<`: removed commit; `>`: added commit.",
            "Commit matching is heuristic; this diff does not prove "
            "semantic equivalence between the two series.",
            "",
        ]
    for conflict in data.get("conflicts", []):
        lines += [
            f"## Conflict {conflict['number']}: {conflict['path']!r}",
            "",
            f"- Status: `{conflict['status']}`",
            f"- Replayed commit / HEAD: `{conflict['head']}`",
        ]
        if "resolution" in conflict:
            resolution = conflict["resolution"]
            lines += [
                f"- Action: `{resolution['action']}`",
                f"- Confidence: {resolution['confidence']}",
                "",
                *["> " + line for line in resolution["rationale"].splitlines()],
                "",
            ]
        for label, path in conflict.get("artifacts", {}).items():
            lines.append(f"- [{label}]({path})")
        if conflict.get("capture_error"):
            lines.append(f"Partial capture: {conflict['capture_error']}")
        lines.append("")
    for session in data.get("sessions", []):
        folder = session["artifacts"]
        lines += [
            f"## Agent session {session['round']}",
            "",
            f"- Status: `{session['status']}` — {session['tool_calls']} tool calls",
            f"- Runtime: `{session['runtime']}`",
            f"- [Agent configuration]({folder}/runtime.json)",
            f"- [Agent files and offloaded history]({folder}/agent-files/)",
            f"- [Conversation]({folder}/messages.json)",
            f"- [Tool events]({folder}/events.jsonl)",
            f"- [Worktree diff]({folder}/worktree.diff)",
            f"- [File state, including new files]({folder}/files.json)",
        ]
        if f"{folder}/staged.diff" in data.get("evidence", {}):
            lines += [
                f"- [Full staged diff]({folder}/staged.diff)",
            ]
        if "rationale" in session:
            lines += [
                f"- Confidence: {session['confidence']}",
                f"- [Decision and rationale]({folder}/decision.json)",
                "",
                *["> " + line for line in session["rationale"].splitlines()],
            ]
        if session.get("capture_error"):
            lines.append(f"Capture failed: {session['capture_error']}")
        lines.append("")
    for review in data.get("reviews", []):
        folder = review["artifacts"]
        lines += [
            f"## Proposal {review['attempt']} (conflict stop {review['round']})",
            "",
            f"- Status: {review['status']}",
            f"- User decision: {review.get('decision', 'unavailable')}",
            f"- Feedback: {review.get('user_feedback') or '—'}",
            f"- [Proposal diff]({folder}/worktree.diff)",
            f"- [File contents and capture limits]({folder}/files.json)",
            f"- Rationale: {review.get('resolution', {}).get('rationale', 'unavailable')}",
            "- Acceptance authorizes engine checks; application is recorded separately.",
            "",
        ]
    for native in data.get("native_resolutions", []):
        lines += ["## Reused Git resolution", "", native["rationale"], ""]
    if data.get("failure"):
        lines += [f"Failure phase: `{data['failure']['phase']}`", ""]
    if data.get("audit_errors"):
        lines += [
            "## Audit errors",
            "",
            "These errors do not replace the original failure.",
            "",
        ]
        for error in data["audit_errors"]:
            lines.append(f"- {error['phase']}: {error['type']}: {error['message']}")
        lines.append("")
    if data.get("evidence"):
        lines += ["## Evidence availability", ""]
        for artifact, evidence in sorted(data["evidence"].items()):
            detail = f" — {evidence['reason']}" if evidence.get("reason") else ""
            lines.append(f"- `{safe_text(artifact)}`: **{evidence['status']}**{detail}")
        lines.append("")
    lines += ["## Git traces", "", "- [Integration log](integration.log)"]
    if data.get("exit_state"):
        lines += [
            "- [Final file state](exit/files.json)",
            "- [Exit diff](exit/worktree.diff)",
        ]
    if data.get("capture_error"):
        lines += [f"Final capture failed: {data['capture_error']}"]
    if data.get("result_oid"):
        lines.append("- [Full before / after diff](result.diff)")
    lines.append("")
    if "validation" in data:
        lines += [
            f"Validation status: `{data['validation'].get('status', 'unknown')}`",
            f"Validation command: `{data['validation']['command']}`",
            f"Exit code: `{data['validation'].get('returncode', 'unavailable')}`",
            "",
        ]

    def link(match: re.Match[str]) -> str:
        path = match.group(2)
        parsed = PurePosixPath(path)
        allowed = path in registry or any(
            name.startswith(path.rstrip("/") + "/") for name in registry
        )
        if parsed.is_absolute() or ".." in parsed.parts or ":" in path or not allowed:
            return match.group(1) + " (evidence unavailable)"
        return match.group(0)

    return re.sub(r"\[([^\]\n]+)\]\(([^)\n]+)\)", link, "\n".join(lines))
