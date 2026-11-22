# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Durable integration manifests, resolution evidence and reports."""

import hashlib
import json
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import EzError
from .git import Git


def now() -> str:
    return datetime.now(UTC).isoformat()


def write_json(path: Path, data: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    temporary.replace(path)


class Audit:
    def __init__(self, directory: Path, manifest: dict[str, Any]):
        self.directory = directory
        self.manifest = manifest
        self.manifest["id"] = directory.name
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.save()

    def save(self) -> None:
        self.manifest["updated_at"] = now()
        write_json(self.directory / "run.json", self.manifest)
        self.report()

    def artifact(self, number: int, filename: str, content: str) -> str:
        folder = self.directory / "conflicts" / f"{number:04d}"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / filename
        target.write_bytes(content.encode("utf-8"))
        return target.relative_to(self.directory).as_posix()

    def capture_worktree(
        self,
        git: Git,
        directory: Path,
        limit: int,
        paths: list[str] | None = None,
        reference: str = "HEAD",
    ) -> None:
        """Archive tracked edits and non-ignored new files without staging or following links."""
        directory.mkdir(parents=True, exist_ok=True)
        diff = git.run(
            "diff", "--no-ext-diff", "--no-textconv", "--binary", reference, "--", check=False
        )
        (directory / "worktree.diff").write_bytes(diff.stdout)
        changed = git.run(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--name-only",
            "-z",
            reference,
            "--",
            check=False,
        )
        untracked = git.run("ls-files", "--others", "--exclude-standard", "-z", check=False)
        names = set(
            (changed.stdout + untracked.stdout)
            .decode("utf-8", errors="surrogateescape")
            .split("\0")
        )
        names.update(paths or [])
        records: list[dict[str, Any]] = []
        for name in sorted(names - {""}):
            record: dict[str, Any] = {"path": name}
            records.append(record)
            try:
                path = git.safe_path(name)
                if not path.exists():
                    record["state"] = "deleted"
                    continue
                info = path.stat()
                if not stat.S_ISREG(info.st_mode):
                    raise EzError("Not a regular file")
                # Retain raw bytes, including files an unfinished agent left invalid or binary.
                with path.open("rb") as stream:
                    content = stream.read(limit + 1)
                truncated = len(content) > limit
                content = content[:limit]
                artifact = Path("files") / f"{len(records):04d}.bin"
                (directory / "files").mkdir(exist_ok=True)
                (directory / artifact).write_bytes(content)
                record.update(
                    state="file",
                    artifact=artifact.as_posix(),
                    size=info.st_size,
                    mode=stat.S_IMODE(info.st_mode),
                    recorded_bytes=len(content),
                    truncated=truncated,
                    recorded_sha256=hashlib.sha256(content).hexdigest(),
                )
            except (EzError, OSError, ValueError) as exc:
                record.update(state="unavailable", error=str(exc))
        head = git.run("rev-parse", "HEAD", check=False)
        branch = git.run("symbolic-ref", "-q", "HEAD", check=False)
        capture_errors = [
            {
                "command": name,
                "returncode": result.returncode,
                "stderr": result.stderr.decode("utf-8", errors="replace"),
            }
            for name, result in (
                ("diff", diff),
                ("changed", changed),
                ("untracked", untracked),
            )
            if result.returncode
        ]
        write_json(
            directory / "files.json",
            {
                "reference": reference,
                "head": head.stdout.decode().strip() or None,
                "branch": branch.stdout.decode().strip() or None,
                "files": records,
                "capture_errors": capture_errors,
            },
        )
        if capture_errors:
            commands = ", ".join(str(error["command"]) for error in capture_errors)
            raise EzError(
                f"Audit capture commands failed: {commands}; inspect {directory / 'files.json'}"
            )

    def report(self) -> None:
        data = self.manifest
        separate_base = (
            "onto" in data or data.get("rebase_plan", {}).get("base_source") == "merge-base"
        )
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
                "`=`: equivalent patch; `!`: modified patch; "
                "`<`: removed commit; `>`: added commit.",
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
                f"- [LangGraph checkpoints]({folder}/checkpoints.sqlite)",
                f"- [Conversation]({folder}/messages.json)",
                f"- [Tool events]({folder}/events.jsonl)",
                f"- [Worktree diff]({folder}/worktree.diff)",
                f"- [File state, including new files]({folder}/files.json)",
            ]
            if (self.directory / folder / "staged.diff").is_file():
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
                f"Validation command: `{data['validation']['command']}`",
                f"Exit code: `{data['validation'].get('returncode', 'interrupted')}`",
                "",
            ]
        (self.directory / "report.md").write_text("\n".join(lines), encoding="utf-8")
