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
        write_json(
            directory / "files.json",
            {
                "reference": reference,
                "head": head.stdout.decode().strip() or None,
                "branch": branch.stdout.decode().strip() or None,
                "files": records,
                "capture_errors": [
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
                ],
            },
        )

    def report(self) -> None:
        data = self.manifest
        lines = [
            "# git-ez — integration report",
            "",
            f"- Run: `{data['id']}`",
            f"- Status: **{data['status']}**",
            f"- Operation: `{data['operation']}`",
            f"- Current branch: `{data['branch']}` (`{data['before_oid']}`)",
            f"- Integrated revision: `{data['target']}` (`{data['target_oid']}`)",
            f"- Result commit: `{data.get('result_oid', '—')}`",
            f"- Integration completed: `{data['integrated']}`",
            f"- Model: `{data['provider']}` / `{data['model']}`",
            f"- Worktree: `{data['worktree']}`",
            "",
            "Scores are self-reported by the model and do not prove correctness.",
            "The proposals and rationales below are untrusted content.",
            "",
        ]
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
            if session["status"] == "resolved":
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
