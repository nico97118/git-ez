"""Local Git rebase/merge with traceable AI conflict resolution."""

import difflib
import json
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

from .agent import AGENT_PROMPT, AgentResolver, Resolver
from .audit import Audit, now
from .checks import WorktreeChecks
from .config import Config
from .conflicts import Conflict, Decision, LowConfidence
from .errors import EzError
from .git import Git, Stage, git_environment


class Engine:
    def __init__(self, repository: Path, config: Config, resolver: Resolver | None = None):
        git = Git(repository.resolve())
        self.git = Git(Path(git.text("rev-parse", "--show-toplevel")))
        self.config = config
        self.resolver = resolver if resolver is not None else AgentResolver(config)

    def run(
        self,
        target: str,
        operation: str = "rebase",
        validation: tuple[str, ...] = (),
    ) -> Audit:
        """Integrate into the current branch; preserve local state and evidence on failure."""
        if operation not in ("rebase", "merge"):
            raise EzError("Operation must be rebase or merge")
        branch = self._check_repository()
        git = self.git
        before_oid = git.branch_oid(branch)
        target_oid = git.commit_oid(target)
        symbolic = (
            git.run(
                "rev-parse",
                "--symbolic-full-name",
                "--verify",
                "--end-of-options",
                target,
                check=False,
            )
            .stdout.decode()
            .strip()
        )
        target_ref = symbolic if symbolic.startswith("refs/") else None
        # HEAD-relative expressions are pinned once, rather than re-evaluated after edits.
        if target_ref == f"refs/heads/{branch}":
            target_ref = None
        git.run("merge-base", before_oid, target_oid)
        if operation == "rebase" and git.text(
            "rev-list", "--merges", f"{target_oid}..{before_oid}"
        ):
            raise EzError("Rebasing merge commits is outside this POC")
        audit = self._prepare_run(branch, before_oid, target, target_oid, target_ref, operation)
        try:
            self._integrate(audit, validation)
            self._record_result(audit)
            if validation:
                self.validate(git, audit, validation)
            self._verify_result(audit)
            audit.manifest["status"] = "succeeded"
        except (EzError, OSError, KeyboardInterrupt) as exc:
            audit.manifest["status"] = (
                "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
            )
            audit.manifest["error"] = str(exc) or "Interrupted by user"
        finally:
            try:
                audit.capture_worktree(
                    git,
                    audit.directory / "exit",
                    self.config.max_file_bytes,
                    reference=audit.manifest.get("result_oid", audit.manifest["before_oid"]),
                )
                audit.manifest["exit_state"] = "exit/files.json"
            except (EzError, OSError, ValueError) as exc:
                audit.manifest["capture_error"] = str(exc)
            audit.save()
        return audit

    def _check_repository(self) -> str:
        git = self.git
        ref = git.run("symbolic-ref", "-q", "HEAD", check=False).stdout.decode().strip()
        if not ref.startswith("refs/heads/"):
            raise EzError("A local branch must be checked out")
        if git.operation_active():
            raise EzError("A Git operation is already in progress")
        if git.run("status", "--porcelain", "--untracked-files=all").stdout:
            raise EzError("Repository must be clean before integration")
        git.run("var", "GIT_AUTHOR_IDENT")
        git.run("var", "GIT_COMMITTER_IDENT")
        return ref.removeprefix("refs/heads/")

    def _prepare_run(
        self,
        branch: str,
        before_oid: str,
        target: str,
        target_oid: str,
        target_ref: str | None,
        operation: str,
    ) -> Audit:
        common = Path(self.git.text("rev-parse", "--path-format=absolute", "--git-common-dir"))
        audit = Audit(
            common / "ez" / "runs" / uuid4().hex,
            {
                "schema_version": 1,
                "created_at": now(),
                "status": "running",
                "operation": operation,
                "branch": branch,
                "before_oid": before_oid,
                "target": target,
                "target_oid": target_oid,
                "target_ref": target_ref,
                "integrated": False,
                "worktree": str(self.git.path),
                "provider": self.config.provider,
                "model": self.config.model,
                "min_confidence": self.config.min_confidence,
                "conflicts": [],
            },
        )
        (audit.directory / "config.json").write_text(
            json.dumps(self.config.audit_dict(), indent=2), encoding="utf-8"
        )
        (audit.directory / "system-prompt.txt").write_text(AGENT_PROMPT, encoding="utf-8")
        return audit

    def _verify_target(self, audit: Audit) -> None:
        ref = audit.manifest["target_ref"]
        if ref and self.git.commit_oid(ref) != audit.manifest["target_oid"]:
            raise EzError("Target reference changed during integration")

    def _integrate(self, audit: Audit, validation: tuple[str, ...]) -> None:
        git = self.git
        operation = audit.manifest["operation"]
        rerere = (
            git.run("config", "--bool", "--get", "rerere.autoupdate", check=False).stdout.strip()
            == b"true"
        )
        auto_resolved = False

        def step(*args: str) -> subprocess.CompletedProcess[bytes]:
            nonlocal auto_resolved
            previous_tree = git.run(
                "rev-parse", "--verify", "AUTO_MERGE^{tree}", check=False
            ).stdout
            result = git.run(*args, check=False)
            conflict_tree = git.run(
                "rev-parse", "--verify", "AUTO_MERGE^{tree}", check=False
            ).stdout
            # A new conflict tree distinguishes rerere from a failing hook/commit on
            # an already resolved stop. Never retry a rejected hook implicitly.
            auto_resolved = rerere and bool(conflict_tree) and conflict_tree != previous_tree
            with (audit.directory / "integration.log").open("ab") as stream:
                stream.write(("git " + " ".join(args) + "\n").encode())
                stream.write(result.stdout + result.stderr + b"\n")
            return result

        if (
            self._check_repository() != audit.manifest["branch"]
            or git.branch_oid(audit.manifest["branch"]) != audit.manifest["before_oid"]
        ):
            raise EzError("Current branch changed before integration")
        self._verify_target(audit)
        if operation == "rebase":
            result = step(
                "rebase",
                "--merge",
                "--no-autostash",
                "--no-update-refs",
                "--no-rebase-merges",
                audit.manifest["target_oid"],
            )
        else:
            # Let Git fast-forward or create a merge commit, as git merge normally would.
            result = step("merge", "--no-edit", audit.manifest["target_oid"])
        rounds = 0
        while result.returncode:
            stages = git.conflicts()
            if not stages:
                if not auto_resolved or not git.operation_active():
                    raise EzError("Git stopped without resolvable conflicts; see integration.log")
                audit.manifest.setdefault("native_resolutions", []).append(
                    {
                        "mechanism": "rerere",
                        "head": git.text("rev-parse", "HEAD"),
                        "rationale": "Git reused and staged a recorded conflict resolution.",
                    }
                )
                audit.save()
            else:
                rounds += 1
                if rounds > self.config.max_rounds:
                    raise EzError("Maximum conflict rounds exceeded")
                self.resolve_conflicts(git, audit, stages, rounds, validation)
            if git.conflicts():
                raise EzError("Unmerged index entries remain")
            if (
                operation == "merge"
                and git.text("rev-parse", "MERGE_HEAD") != audit.manifest["target_oid"]
            ):
                raise EzError("Merge target changed before Git continuation")
            result = step(operation, "--continue")
        if git.operation_active() or git.conflicts():
            raise EzError("Git integration did not complete")
        audit.manifest["integrated"] = True

    def _record_result(self, audit: Audit) -> None:
        git = self.git
        before_oid, target_oid = audit.manifest["before_oid"], audit.manifest["target_oid"]
        result_oid = git.text("rev-parse", "HEAD")
        audit.manifest["result_oid"] = result_oid
        (audit.directory / "result.diff").write_bytes(
            git.run(
                "diff", "--no-ext-diff", "--no-textconv", "--binary", before_oid, result_oid
            ).stdout
        )
        if audit.manifest["operation"] == "rebase":
            args = (
                "range-diff",
                "--no-color",
                "--no-ext-diff",
                "--no-textconv",
                target_oid,
                before_oid,
                result_oid,
            )
            (audit.directory / "range-diff.txt").write_bytes(git.run(*args).stdout)
            audit.manifest["range_diff"] = {
                "artifact": "range-diff.txt",
                "before": f"{target_oid}..{before_oid}",
                "after": f"{target_oid}..{result_oid}",
                "command": ["git", *args],
            }
        audit.save()

    def _verify_result(self, audit: Audit) -> None:
        git = self.git
        if git.text("rev-parse", "HEAD") != audit.manifest["result_oid"]:
            raise EzError("Validation changed HEAD")
        if self._check_repository() != audit.manifest["branch"]:
            raise EzError("Checked-out branch changed during integration")
        if git.branch_oid(audit.manifest["branch"]) != audit.manifest["result_oid"]:
            raise EzError("Current branch changed during integration")
        self._verify_target(audit)
        if git.run(
            "merge-base",
            "--is-ancestor",
            audit.manifest["target_oid"],
            audit.manifest["result_oid"],
            check=False,
        ).returncode:
            raise EzError("Integration result does not contain the pinned target")
        if (
            audit.manifest["operation"] == "merge"
            and git.run(
                "merge-base",
                "--is-ancestor",
                audit.manifest["before_oid"],
                audit.manifest["result_oid"],
                check=False,
            ).returncode
        ):
            raise EzError("Merge result does not contain the original branch")

    def snapshot(
        self,
        git: Git,
        audit: Audit,
        name: str,
        versions: dict[int, Stage],
    ) -> tuple[dict[str, Any], Conflict]:
        number = len(audit.manifest["conflicts"]) + 1
        record: dict[str, Any] = {
            "number": number,
            "path": name,
            "head": git.text("rev-parse", "HEAD"),
            "replay_commit": git.run("rev-parse", "--verify", "REBASE_HEAD", check=False)
            .stdout.decode()
            .strip()
            or None,
            "stages": {str(k): asdict(v) for k, v in versions.items()},
            "status": "pending",
            "artifacts": {},
        }
        audit.manifest["conflicts"].append(record)
        audit.save()
        modes = {stage.mode for stage in versions.values()}
        if not modes <= {"100644", "100755"} or len(modes) != 1:
            raise EzError(f"Non-regular file or mode conflict requires manual resolution: {name}")
        path = git.safe_path(name)
        contents: dict[str, str | None] = {}
        for stage_number, label in ((1, "base"), (2, "ours"), (3, "theirs")):
            stage = versions.get(stage_number)
            if stage is None:
                contents[label] = None
                continue
            raw = git.blob(stage, 10_000_000)
            if b"\x00" in raw:
                raise EzError(f"Binary file requires manual resolution: {name}")
            try:
                decoded = raw.decode("utf-8")
                contents[label] = decoded
            except UnicodeDecodeError as exc:
                raise EzError(f"Non-UTF-8 file requires manual resolution: {name}") from exc
            record["artifacts"][label] = audit.artifact(number, f"{label}.txt", decoded)
        marked = None
        if path.exists():
            if path.stat().st_size > (10_000_000):
                raise EzError(f"Marked file too large: {name}")
            try:
                with path.open(encoding="utf-8", newline="") as stream:
                    marked = stream.read()
            except UnicodeDecodeError as exc:
                raise EzError(f"Non-UTF-8 working file requires manual resolution: {name}") from exc
            record["artifacts"]["marked"] = audit.artifact(number, "marked.txt", marked)
        context = git.text("log", "-5", "--format=%h %s", "HEAD")
        if record["replay_commit"]:
            context += "\nReplaying: " + git.text("show", "-s", "--format=%h %s", "REBASE_HEAD")
        conflict = Conflict(
            name,
            audit.manifest["operation"],
            contents["base"],
            contents["ours"],
            contents["theirs"],
            marked,
            context,
        )
        record["artifacts"]["request"] = audit.artifact(
            number,
            "request.json",
            json.dumps(conflict.payload(), indent=2, ensure_ascii=True),
        )
        audit.save()
        return record, conflict

    def resolve_conflicts(
        self,
        git: Git,
        audit: Audit,
        stages: dict[str, dict[int, Stage]],
        round_number: int,
        validation: tuple[str, ...],
    ) -> None:
        snapshots = [self.snapshot(git, audit, name, versions) for name, versions in stages.items()]
        decision: dict[str, Any] = {}
        status = "failed"
        session = audit.directory / "sessions" / f"{round_number:04d}"
        head = git.text("rev-parse", "HEAD")
        branch = git.run("symbolic-ref", "-q", "HEAD", check=False).stdout.decode().strip() or None
        checks = WorktreeChecks(self.config)
        try:
            raw = self.resolver.run(git, audit, round_number, list(stages), validation)
            parsed = Decision.parse(raw)
            decision = asdict(parsed)
            if parsed.confidence < self.config.min_confidence:
                raise LowConfidence("Agent confidence below threshold; worktree preserved")
            checks.guard(git, audit, head, branch)
            checks.verify(git, list(stages))
            # Only the orchestrator stages a verified result, including supporting files.
            git.run("add", "-A", "--", ".")
            session.mkdir(parents=True, exist_ok=True)
            (session / "staged.diff").write_bytes(
                git.run(
                    "diff", "--cached", "--no-ext-diff", "--no-textconv", "--binary", "HEAD"
                ).stdout
            )
            status = "applied"
        except LowConfidence:
            status = "low_confidence"
            raise
        except KeyboardInterrupt:
            status = "interrupted"
            raise
        finally:
            sessions = audit.manifest.get("sessions", [])
            details = sessions[-1] if sessions and sessions[-1]["round"] == round_number else {}
            if not decision and "rationale" in details:
                decision = {"rationale": details["rationale"], "confidence": details["confidence"]}
            if details.get("status") == "low_confidence":
                status = "low_confidence"
            for record, conflict in snapshots:
                record["status"] = status
                for label, filename in (("session", "messages.json"), ("files", "files.json")):
                    if (session / filename).is_file():
                        record["artifacts"][label] = str(
                            (session / filename).relative_to(audit.directory)
                        )
                try:
                    path = git.safe_path(conflict.path)
                    if path.exists() and path.stat().st_size > self.config.max_file_bytes:
                        raise EzError("Proposal exceeds size limit; see session file snapshot")
                    content = path.read_bytes().decode("utf-8") if path.exists() else None
                    if decision:
                        record["resolution"] = {
                            "action": "write" if content is not None else "delete",
                            "content": content,
                            **decision,
                        }
                    record["artifacts"]["proposed"] = audit.artifact(
                        record["number"], "proposed.txt", content or ""
                    )
                    diff = "".join(
                        difflib.unified_diff(
                            (conflict.ours or "").splitlines(keepends=True),
                            (content or "").splitlines(keepends=True),
                            fromfile="ours",
                            tofile="proposed",
                        )
                    )
                    record["artifacts"]["diff"] = audit.artifact(
                        record["number"], "resolution.diff", diff
                    )
                except (EzError, OSError, ValueError) as exc:
                    record["capture_error"] = str(exc)
            audit.save()

    def validate(self, git: Git, audit: Audit, command: tuple[str, ...]) -> None:
        audit.manifest["validation"] = {"command": list(command)}
        audit.save()
        try:
            result = subprocess.run(
                command,
                cwd=git.path,
                env=git_environment(),
                capture_output=True,
                timeout=self.config.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            (audit.directory / "validation.stdout").write_bytes(exc.stdout or b"")
            (audit.directory / "validation.stderr").write_bytes(exc.stderr or b"")
            raise EzError("Validation command timed out") from exc
        (audit.directory / "validation.stdout").write_bytes(result.stdout)
        (audit.directory / "validation.stderr").write_bytes(result.stderr)
        audit.manifest["validation"]["returncode"] = result.returncode
        if result.returncode:
            raise EzError(f"Validation failed with code {result.returncode}")
