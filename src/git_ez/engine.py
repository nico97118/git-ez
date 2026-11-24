# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Local Git rebase/merge with traceable AI conflict resolution."""

import hashlib
import json
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import asdict
from functools import partial
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from .agent import AgentResolver, Resolver
from .audit import Audit, Record, Validation, now, safe_error
from .audit_safety import redact_bytes
from .audit_storage import read_regular as read_evidence
from .checks import WorktreeChecks
from .config import Config
from .conflict_state import ConflictState, uninterrupted
from .conflicts import Conflict, Decision, LowConfidence, text_diff
from .errors import AuditError, EzError
from .git import Git, Stage, git_environment
from .locking import checkout_lock
from .options import CommitOptions, RebaseOptions
from .processes import ChildCleanupError, interrupt_children, run_process
from .review import capture_proposal, review_proposal
from .sequence_editor import editor_command, read_regular


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
        *,
        merge_ff: Literal["ff", "no-ff", "ff-only"] | None = None,
        onto: str | None = None,
        rebase_options: RebaseOptions | None = None,
        commit_options: CommitOptions | None = None,
        rerere_autoupdate: bool | None = None,
        interactive: bool = False,
    ) -> Audit:
        """Integrate into the current branch; preserve local state and evidence on failure."""
        if operation not in ("rebase", "merge"):
            raise EzError("Operation must be rebase or merge")
        if onto is not None and operation != "rebase":
            raise EzError("--onto is only supported for rebase")
        if rebase_options is not None and operation != "rebase":
            raise EzError("Rebase options are only supported for rebase")
        options = rebase_options or RebaseOptions()
        if options.keep_base and onto is not None:
            raise EzError("--keep-base and --onto cannot be used together")
        commit_options = commit_options or CommitOptions()
        if operation != "merge" and (
            commit_options.message is not None
            or commit_options.verify_signatures is not None
            or commit_options.log is not None
            or commit_options.cleanup is not None
        ):
            raise EzError(
                "Messages, log, cleanup and signature verification are only supported for merge"
            )
        if merge_ff is not None:
            if operation != "merge":
                raise EzError("Fast-forward policy is only supported for merge")
            if merge_ff not in ("ff", "no-ff", "ff-only"):
                raise EzError("Merge fast-forward policy must be ff, no-ff or ff-only")
        with checkout_lock(self.git):
            branch = self._check_repository()
            git = self.git
            before_oid = git.branch_oid(branch)
            target_oid = git.commit_oid(target)
            target_ref = self._input_ref(target, branch)
            onto_oid = git.commit_oid(onto) if onto is not None else None
            onto_ref = self._input_ref(onto, branch) if onto is not None else None
            if operation != "rebase" or not options.keep_base:
                git.run("merge-base", before_oid, target_oid)
            plan = None
            if operation == "rebase":
                plan = self._plan_rebase(target, target_oid, before_oid, onto_oid, branch, options)
                exclusions = [plan["upstream_oid"]]
                if plan["fork_point"]["oid"] is not None:
                    exclusions.append(plan["fork_point"]["oid"])
                if git.text("rev-list", "--merges", before_oid, "--not", *exclusions):
                    raise EzError("Rebasing merge commits is outside this POC")
            audit = self._prepare_run(branch, before_oid, target, target_oid, target_ref, operation)
            if onto is not None:
                audit.annotate(onto=onto, onto_oid=onto_oid, onto_ref=onto_ref)
            if operation == "rebase":
                audit.annotate(rebase_options=asdict(options))
                audit.annotate(rebase_plan=plan)
            if operation == "merge":
                audit.annotate(merge_ff=merge_ff)
            audit.annotate(commit_options=asdict(commit_options))
            audit.annotate(rerere_autoupdate=rerere_autoupdate)
            audit.annotate(interactive=interactive)
            audit.annotate(
                validation=Validation(
                    command=list(validation), status="not_run" if validation else "not_requested"
                )
            )
            phase = "initialization"
            try:
                audit.initialize()
                phase = "integration"
                self._integrate(audit, validation)
                if validation:
                    phase = "validation"
                    self.validate(git, audit, validation)
                phase = "verification"
                self._verify_result(audit)
                audit.set_status("succeeded")
            except (EzError, OSError, ValueError, KeyboardInterrupt) as exc:
                audit.set_status(
                    "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                    exc,
                    phase=phase,
                )
            except BaseException as exc:
                # Preserve unexpected exceptions and termination signals for the caller.
                # Their arbitrary messages may contain credentials; retain only their type.
                audit.set_status(
                    "interrupted" if isinstance(exc, SystemExit) else "failed",
                    exc,
                    phase=phase,
                    message=f"Unexpected {type(exc).__name__}",
                )
                raise
            finally:
                if "rebase_sequence" in audit.view:
                    sequence_error = audit.attempt(
                        "sequence guard", lambda: self._verify_sequence_capture(audit)
                    )
                    if sequence_error is not None and audit.view["status"] == "succeeded":
                        audit.set_status("failed", sequence_error, phase="verification")
                audit.finalize()
            return audit

    def _input_ref(
        self, revision: str, branch: str, *, include_current: bool = False
    ) -> str | None:
        symbolic = (
            self.git.run(
                "rev-parse",
                "--symbolic-full-name",
                "--verify",
                "--end-of-options",
                revision,
                check=False,
            )
            .stdout.decode()
            .strip()
        )
        # HEAD-relative expressions are pinned once, rather than re-evaluated after edits.
        if symbolic.startswith("refs/") and (include_current or symbolic != f"refs/heads/{branch}"):
            return symbolic
        return None

    def _plan_rebase(
        self,
        target: str,
        target_oid: str,
        before_oid: str,
        onto_oid: str | None,
        branch: str,
        options: RebaseOptions,
    ) -> dict[str, Any]:
        git = self.git
        base_oid = onto_oid or target_oid
        if options.keep_base:
            common = git.run("merge-base", "--all", target_oid, before_oid, check=False)
            if common.returncode not in (0, 1):
                raise EzError(
                    "Cannot determine rebase base: "
                    + common.stderr.decode(errors="replace").strip()
                )
            bases = common.stdout.decode().strip().splitlines()
            if len(bases) != 1:
                raise EzError("--keep-base requires exactly one merge base with the upstream")
            base_oid = bases[0]
        configured = git.run("config", "--bool", "--get", "rebase.forkPoint", check=False)
        if configured.returncode not in (0, 1):
            raise EzError(
                "Cannot read rebase.forkPoint: "
                + configured.stderr.decode(errors="replace").strip()
            )
        # An explicit upstream defaults to --no-fork-point in native Git, even
        # with rebase.forkPoint=true. Only an explicit CLI request enables it.
        fork_enabled = options.fork_point is True
        reapply = options.reapply_cherry_picks
        if reapply is None:
            reapply = options.keep_base
        fork: dict[str, Any] = {
            "enabled": fork_enabled,
            "oid": None,
            "source_ref": None,
            "status": "disabled",
        }
        if fork_enabled:
            source_ref = self._input_ref(target, branch, include_current=True)
            if source_ref is None:
                raise EzError(
                    "--fork-point requires a named upstream reference; "
                    "use --no-fork-point for a revision expression"
                )
            point = git.run("merge-base", "--fork-point", source_ref, before_oid, check=False)
            if point.returncode not in (0, 1):
                raise EzError(
                    "Cannot determine fork point: " + point.stderr.decode(errors="replace").strip()
                )
            fork.update(
                source_ref=source_ref, status="found" if point.returncode == 0 else "fallback"
            )
            diagnostic = point.stderr.decode(errors="replace").strip()
            if diagnostic:
                fork["diagnostic"] = diagnostic
            if point.returncode == 0:
                fork["oid"] = git.commit_oid(point.stdout.decode().strip())
            else:
                fork["reason"] = (
                    "No usable reflog fork point; retaining the pinned replay boundary."
                )
        return {
            "base_oid": base_oid,
            "base_source": "merge-base"
            if options.keep_base
            else "onto"
            if onto_oid
            else "upstream",
            "upstream_oid": base_oid if options.keep_base and reapply else target_oid,
            "reapply_cherry_picks": reapply,
            "fork_point": fork,
        }

    def _pin_rebase_selection(self, audit: Audit) -> None:
        """Represent the union of pinned exclusions without introducing any refs."""
        plan = audit.view["rebase_plan"]
        upstream = plan["upstream_oid"]
        fork = plan["fork_point"]["oid"]
        selection = upstream
        if fork is not None:
            if (
                self.git.run("merge-base", "--is-ancestor", upstream, fork, check=False).returncode
                == 0
            ):
                selection = fork
            elif self.git.run(
                "merge-base", "--is-ancestor", fork, upstream, check=False
            ).returncode:
                # Replacing upstream with fork alone would lose upstream's patch
                # equivalence checks. This merge object preserves both exclusions;
                # its own patch is ignored by Git's non-merge replay selection.
                tree = self.git.text("rev-parse", f"{upstream}^{{tree}}")
                args = (
                    "-c",
                    "commit.gpgSign=false",
                    "commit-tree",
                    tree,
                    "-p",
                    upstream,
                    "-p",
                    fork,
                    "-m",
                    "git-ez pinned rebase selection",
                )
                selection = self.git.text(*args)
        plan["selection_oid"] = selection
        plan["requires_sequence"] = (
            fork is not None and selection != upstream and plan["base_oid"] == fork
        )
        audit.annotate(rebase_plan=plan)
        audit.save()

    def _check_repository(self) -> str:
        git = self.git
        ref = git.run("symbolic-ref", "-q", "HEAD", check=False).stdout.decode().strip()
        if not ref.startswith("refs/heads/"):
            raise EzError("A local branch must be checked out")
        if git.operation_active():
            raise EzError("A Git operation is already in progress")
        if git.conflicts():
            raise EzError("Unmerged index entries require manual resolution")
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
        git_directory = self.git.text("rev-parse", "--absolute-git-dir")
        audit = Audit(
            common / "ez" / "runs" / uuid4().hex,
            {
                "schema_version": 4,
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
        audit.annotate(
            git_directory=git_directory,
            common_git_directory=str(common),
        )
        audit.protect(self.config.resolved_api_key())
        return audit

    def _verify_target(self, audit: Audit) -> None:
        for name in ("target", "onto"):
            ref = audit.view.get(f"{name}_ref")
            if ref and self.git.commit_oid(ref) != audit.view[f"{name}_oid"]:
                raise EzError(f"{name.capitalize()} reference changed during integration")

    def _step(
        self,
        git: Git,
        audit: Audit,
        command: tuple[str, ...],
        *,
        sequence_editor: str | None = None,
        expected_stop: Callable[[], bool] | None = None,
        inherited_fds: tuple[int, ...] = (),
    ) -> subprocess.CompletedProcess[bytes]:
        result = git.run(
            *command, check=False, sequence_editor=sequence_editor, inherited_fds=inherited_fds
        )
        if result.returncode and (expected_stop is None or not expected_stop()):
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise EzError(f"Native Git command failed ({result.returncode}): {detail}")
        return result

    def _integrate(self, audit: Audit, validation: tuple[str, ...]) -> None:
        git = self.git
        operation = audit.view["operation"]
        rebase_options = RebaseOptions(**audit.view.get("rebase_options", {}))
        commit_options = CommitOptions(**audit.view["commit_options"])
        requested_rerere = audit.view["rerere_autoupdate"]
        rerere = requested_rerere
        rerere_config: tuple[str, ...] = ()
        if requested_rerere is None:
            rerere = (
                git.run(
                    "config", "--bool", "--get", "rerere.autoupdate", check=False
                ).stdout.strip()
                == b"true"
            )
        else:
            # Scope the CLI override to every Git step, including continuation.
            rerere_config = ("-c", f"rerere.autoupdate={'true' if rerere else 'false'}")
        audit.annotate(rerere_autoupdate_effective=rerere)
        auto_resolved = False

        def step(
            *args: str, sequence_editor: str | None = None, inherited_fds: tuple[int, ...] = ()
        ) -> subprocess.CompletedProcess[bytes]:
            nonlocal auto_resolved
            previous_tree = git.run(
                "rev-parse", "--verify", "AUTO_MERGE^{tree}", check=False
            ).stdout
            auto_resolved = False
            command = (*rerere_config, *args)

            def expected_stop() -> bool:
                nonlocal auto_resolved
                if (
                    operation == "rebase"
                    and sequence_mode
                    and redactions is not None
                    and not redactions.closed
                ):
                    redactions.seek(0)
                    try:
                        helper_result = json.load(redactions)
                    except (OSError, ValueError):
                        helper_result = None
                    if isinstance(helper_result, dict) and helper_result.get("phase") == "failed":
                        audit.set_status(
                            "failed",
                            AuditError("Native sequence evidence capture failed"),
                            phase="sequence_capture",
                        )
                conflict_tree = git.run(
                    "rev-parse", "--verify", "AUTO_MERGE^{tree}", check=False
                ).stdout
                auto_resolved = rerere and bool(conflict_tree) and conflict_tree != previous_tree
                return bool(git.conflicts()) or (auto_resolved and git.operation_active())

            return self._step(
                git,
                audit,
                command,
                sequence_editor=sequence_editor,
                expected_stop=expected_stop if args[0] in ("rebase", "merge", "-c") else None,
                inherited_fds=inherited_fds,
            )

        if (
            self._check_repository() != audit.view["branch"]
            or git.branch_oid(audit.view["branch"]) != audit.view["before_oid"]
        ):
            raise EzError("Current branch changed before integration")
        self._verify_target(audit)
        if operation == "rebase":
            self._pin_rebase_selection(audit)
            plan = audit.view["rebase_plan"]
            flags: tuple[str, ...] = ("--onto", plan["base_oid"], "--no-fork-point")
            if rebase_options.reapply_cherry_picks is None and plan["reapply_cherry_picks"]:
                flags += ("--reapply-cherry-picks",)
            sequence_mode = rebase_options.autosquash is True or plan["requires_sequence"]
            if plan["requires_sequence"] and rebase_options.autosquash is not True:
                # Avoid a false up-to-date shortcut after combining exclusions,
                # while retaining Git's per-commit fast-forward behavior.
                flags += ("--interactive", "--no-autosquash")
                if rebase_options.empty is None:
                    flags += ("--empty=drop",)
            sequence_editor = None
            prefix: tuple[str, ...] = ()
            if sequence_mode:
                sequence = audit.directory / "rebase-todo.txt"
                sequence_evidence = {
                    "artifact": sequence.name,
                    "digest_artifact": sequence.name + ".sha256",
                    "metadata_artifact": sequence.name + ".capture.json",
                    "status": "pending",
                    "limit_bytes": self.config.max_file_bytes,
                }
                audit.annotate(rebase_sequence=sequence_evidence)
                audit.save()
                # Prevent a configured multiline instruction format from adding
                # instructions to the internally generated interactive sequence.
                prefix = ("-c", "rebase.instructionFormat=%s")
            try:
                # Secrets travel only in an inherited, unlinked descriptor. They are
                # never command arguments, environment values, or audit artifacts.
                with tempfile.TemporaryFile() if sequence_mode else nullcontext(None) as redactions:
                    if redactions is not None:
                        redactions.write(json.dumps([self.config.resolved_api_key()]).encode())
                        redactions.seek(0)
                        sequence_editor = editor_command(
                            audit.directory / "rebase-todo.txt",
                            self.config.max_file_bytes,
                            redactions.fileno(),
                        )
                    result = step(
                        *prefix,
                        "rebase",
                        "--merge",
                        "--no-autostash",
                        "--no-update-refs",
                        "--no-rebase-merges",
                        *flags,
                        *rebase_options.flags(),
                        *commit_options.flags(),
                        plan["selection_oid"],
                        sequence_editor=sequence_editor,
                        inherited_fds=(redactions.fileno(),) if redactions is not None else (),
                    )
            finally:
                if sequence_mode:
                    evidence = audit.view["rebase_sequence"]
                    try:
                        content = read_regular(
                            audit.directory / evidence["artifact"], self.config.max_file_bytes
                        )
                        digest = read_regular(
                            audit.directory / evidence["digest_artifact"], 64
                        ).decode("ascii")
                        if hashlib.sha256(content).hexdigest() != digest:
                            raise AuditError("Captured sequence changed before Git returned")
                    except (EzError, OSError, ValueError) as exc:
                        evidence.update(status="unavailable", error=safe_error(exc))
                    else:
                        capture_metadata = json.loads(
                            read_evidence(audit.directory / evidence["metadata_artifact"])
                        )
                        evidence.update(
                            redacted=capture_metadata["redacted"],
                            source_sha256=capture_metadata["source_sha256"],
                            status="captured",
                            sha256=digest,
                            size_bytes=len(content),
                        )
                    audit.annotate(rebase_sequence=evidence)
                    audit.save()
        else:
            # Let Git fast-forward or create a merge commit, as git merge normally would.
            merge_ff = audit.view["merge_ff"]
            flags = (f"--{merge_ff}",) if merge_ff is not None else ()
            result = step(
                "merge",
                "--no-autostash",
                "--no-edit",
                *flags,
                *commit_options.flags(),
                audit.view["target_oid"],
            )
        rounds = 0
        while result.returncode:
            stages = git.conflicts()
            if not stages:
                if not auto_resolved or not git.operation_active():
                    raise EzError("Git stopped without resolvable conflicts; inspect Git state")
            else:
                rounds += 1
                if rounds > self.config.max_rounds:
                    raise EzError("Maximum conflict rounds exceeded")
                self.resolve_conflicts(git, audit, stages, rounds, validation)
            if git.conflicts():
                raise EzError("Unmerged index entries remain")
            if (
                operation == "rebase"
                and rebase_options.empty is not None
                and git.run("diff", "--cached", "--quiet", "HEAD", check=False).returncode == 0
                and not (
                    rebase_options.autosquash is True
                    and self._rebase_action() in ("fixup", "squash")
                )
            ):
                replay_oid = git.text("rev-parse", "REBASE_HEAD")
                audit.add_record(
                    "empty_commits",
                    {
                        "commit": replay_oid,
                        "policy": rebase_options.empty,
                        "reason": "Resolution left no staged changes relative to HEAD.",
                    },
                )
                audit.save()
                if rebase_options.empty == "stop":
                    raise EzError(
                        "Rebase resolution is empty; inspect the audit and use native Git "
                        "to commit with --allow-empty or skip the commit"
                    )
                if rebase_options.empty == "keep":
                    self._verify_target(audit)
                    committed = step(
                        "commit",
                        "--allow-empty",
                        "--allow-empty-message",
                        "--no-edit",
                        "-C",
                        replay_oid,
                        *commit_options.flags(),
                    )
                    if committed.returncode:
                        raise EzError("Cannot retain empty commit; inspect Git state")
            if (
                operation == "merge"
                and git.text("rev-parse", "MERGE_HEAD") != audit.view["target_oid"]
            ):
                raise EzError("Merge target changed before Git continuation")
            if operation == "merge" and commit_options.flags(verify=False):
                # Keep Git's prepared shortlog and cleanup markers, including
                # summaries enabled through merge.log rather than CLI options.
                configured_log = git.run(
                    "config", "--type=bool-or-int", "--get", "merge.log", check=False
                ).stdout.strip()
                prepared_message = (
                    commit_options.log is not None
                    or commit_options.cleanup is not None
                    or configured_log not in (b"", b"false", b"0")
                )
                result = step(
                    "commit",
                    "--no-edit",
                    *commit_options.flags(verify=False, prepared_message=prepared_message),
                )
            else:
                result = step(operation, "--continue")
        if git.operation_active() or git.conflicts():
            raise EzError("Git integration did not complete")
        audit.completed(git.text("rev-parse", "HEAD"))

    def _verify_sequence_capture(self, audit: Audit) -> None:
        evidence = audit.view["rebase_sequence"]
        try:
            if evidence["status"] != "captured":
                raise EzError(evidence.get("error", "Initial sequence capture was unavailable"))
            content = read_regular(
                audit.directory / evidence["artifact"], self.config.max_file_bytes
            )
            if hashlib.sha256(content).hexdigest() != evidence["sha256"]:
                raise EzError("Captured sequence changed during rebase continuation")
            digest = read_regular(audit.directory / evidence["digest_artifact"], 64).decode("ascii")
            if digest != evidence["sha256"]:
                raise EzError("Captured sequence fingerprint changed during rebase continuation")
        except (EzError, OSError, ValueError) as exc:
            evidence.update(status="unavailable", error=safe_error(exc))
            audit.annotate(rebase_sequence=evidence)
            raise EzError(
                "Required rebase sequence capture is unavailable; inspect the audit"
            ) from exc

    def _rebase_action(self) -> str:
        path = Path(self.git.text("rev-parse", "--git-path", "rebase-merge/done"))
        if not path.is_absolute():
            path = self.git.path / path
        lines = read_regular(path, self.config.max_file_bytes).splitlines()
        actions = {
            b"pick": "pick",
            b"p": "pick",
            b"fixup": "fixup",
            b"f": "fixup",
            b"squash": "squash",
            b"s": "squash",
        }
        for line in reversed(lines):
            if line.strip() and not line.lstrip().startswith(b"#"):
                action = actions.get(line.split()[0])
                if action is not None:
                    return action
                break
        raise EzError("Cannot determine the current autosquash action; inspect the rebase state")

    def _verify_result(self, audit: Audit) -> None:
        git = self.git
        if git.text("rev-parse", "HEAD") != audit.view["result_oid"]:
            raise EzError("Validation changed HEAD")
        if self._check_repository() != audit.view["branch"]:
            raise EzError("Checked-out branch changed during integration")
        if git.branch_oid(audit.view["branch"]) != audit.view["result_oid"]:
            raise EzError("Current branch changed during integration")
        self._verify_target(audit)
        if git.run(
            "merge-base",
            "--is-ancestor",
            audit.view.get("rebase_plan", {}).get("base_oid", audit.view["target_oid"]),
            audit.view["result_oid"],
            check=False,
        ).returncode:
            raise EzError("Integration result does not contain the pinned target or new base")
        if (
            audit.view["operation"] == "merge"
            and git.run(
                "merge-base",
                "--is-ancestor",
                audit.view["before_oid"],
                audit.view["result_oid"],
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
        stop_id: str | None = None,
    ) -> tuple[Record, Conflict]:
        number = len(audit.view["conflicts"]) + 1
        record = audit.add_record(
            "conflicts",
            {
                "number": number,
                "path": name,
                "head": git.text("rev-parse", "HEAD"),
                "status": "pending",
                "artifacts": {},
                "stop_id": stop_id or uuid4().hex,
            },
        )
        modes = {stage.mode for stage in versions.values()}
        if not modes <= {"100644", "100755"} or len(modes) != 1:
            raise EzError(f"Non-regular file or mode conflict requires manual resolution: {name}")
        contents: dict[str, str | None] = {}
        for stage_number, label in ((1, "base"), (2, "ours"), (3, "theirs")):
            raw = git.blob(versions[stage_number], 10_000_000) if stage_number in versions else None
            if raw is not None and b"\x00" in raw:
                raise EzError(f"Binary file requires manual resolution: {name}")
            try:
                contents[label] = raw.decode("utf-8") if raw is not None else None
            except UnicodeDecodeError as exc:
                raise EzError(f"Non-UTF-8 file requires manual resolution: {name}") from exc
        path = git.safe_path(name)
        try:
            raw_marked = read_regular(path, 10_000_001) if path.exists() else None
            if raw_marked is not None and len(raw_marked) > 10_000_000:
                raise EzError(f"Marked file too large: {name}")
            marked = raw_marked.decode("utf-8") if raw_marked is not None else None
        except UnicodeDecodeError as exc:
            raise EzError(f"Non-UTF-8 working file: {name}") from exc
        conflict = Conflict(
            name,
            audit.operation,
            contents["base"],
            contents["ours"],
            contents["theirs"],
            marked,
            "",
        )
        request = f"conflicts/{number:04d}/request.json"
        audit.write_json(request, conflict.payload())
        record.artifact("request", request)
        diff = text_diff(conflict.ours, conflict.theirs, f"ours/{name}", f"theirs/{name}")
        record.artifact("conflict_diff", audit.artifact(number, "conflict.diff", diff))
        audit.save()
        return record, conflict

    def _save_resolution(
        self,
        git: Git,
        audit: Audit,
        record: Record,
        conflict: Conflict,
        decision: Decision | None,
        directory: str | None = None,
    ) -> None:
        record.update(
            resolution={
                "action": "unavailable",
                "content": None,
                **(asdict(decision) if decision is not None else {}),
            }
        )
        path = git.safe_path(conflict.path)
        content = (
            read_evidence(path, self.config.max_file_bytes + 1).decode("utf-8")
            if path.exists()
            else None
        )
        if content is not None and len(content.encode()) > self.config.max_file_bytes:
            raise EzError("Proposal exceeds size limit")
        record.update(
            resolution={
                "action": "write" if content is not None else "delete",
                "content": content,
                **(asdict(decision) if decision is not None else {}),
            }
        )
        prefix = directory or f"conflicts/{record['number']:04d}"
        audit.write(f"{prefix}/resolution.txt", (content or "").encode())
        record.artifact("proposed", f"{prefix}/resolution.txt")
        diff = text_diff(conflict.ours, content, "ours", "resolution")
        audit.write(f"{prefix}/resolution.diff", diff.encode())
        record.artifact("diff", f"{prefix}/resolution.diff")

    def _capture_attempt(
        self, git: Git, audit: Audit, proposal: Record, decision: Decision | None
    ) -> None:
        captures = []
        for conflict in audit.view["conflicts"]:
            if conflict["id"] not in proposal["conflict_ids"]:
                continue
            payload = json.loads(read_evidence(audit.directory / conflict["artifacts"]["request"]))
            record = Record(audit, {"number": conflict["number"], "path": conflict["path"]})
            self._save_resolution(
                git,
                audit,
                record,
                Conflict(**payload),
                decision,
                f"{proposal['artifacts']}/conflicts/{conflict['number']:04d}",
            )
            captures.append(record.data)
        proposal.update(conflicts=captures)
        audit.save()

    def _capture_changes(
        self,
        git: Git,
        audit: Audit,
        proposal: Record,
        paths: list[str],
        baseline: dict[str, tuple[int, str] | None],
    ) -> list[str]:
        current = WorktreeChecks(self.config).baseline(git)
        selected = sorted(
            set(paths)
            | {
                name
                for name in baseline.keys() | current.keys()
                if baseline.get(name) != current.get(name)
            }
        )
        capture_proposal(
            audit,
            git,
            audit.directory / proposal["artifacts"],
            self.config.max_file_bytes,
            selected,
        )
        return selected

    def resolve_conflicts(
        self,
        git: Git,
        audit: Audit,
        stages: dict[str, dict[int, Stage]],
        round_number: int,
        validation: tuple[str, ...],
    ) -> None:
        if not audit.view.get("interactive"):
            self._resolve_conflicts(git, audit, stages, round_number, validation)
            return
        state = ConflictState(git)
        conflict_count = len(audit.view["conflicts"])
        review_count = len(audit.view.get("reviews", []))
        state.retained = True
        with interrupt_children():
            try:
                self._resolve_conflicts(git, audit, stages, round_number, validation, state)
                state.retained = False
            except ChildCleanupError as exc:
                state.retained = True
                raise EzError(
                    f"Child cleanup failed; conflict was not restored. Backup retained at "
                    f"{state.directory}: {exc}"
                ) from exc
            except BaseException:
                with uninterrupted():
                    state.restore()
                    state.retained = False
                for index in range(conflict_count, len(audit.view["conflicts"])):
                    audit.record("conflicts", index).update(
                        restoration="original_conflict_restored", application="unmerged"
                    )
                for index in range(review_count, len(audit.view.get("reviews", []))):
                    review = audit.record("reviews", index)
                    review.update(restoration="original_conflict_restored")
                    if review.get("application") == "staged":
                        review.update(application="restored_unmerged")
                audit.attempt("conflict restoration metadata", audit.save)
                raise
            finally:
                # Delete only after approval or a completed restoration.
                if not state.retained:
                    state.close()

    def _resolve_conflicts(
        self,
        git: Git,
        audit: Audit,
        stages: dict[str, dict[int, Stage]],
        round_number: int,
        validation: tuple[str, ...],
        state: ConflictState | None = None,
    ) -> None:
        snapshots = [self.snapshot(git, audit, name, versions) for name, versions in stages.items()]
        head = git.text("rev-parse", "HEAD")
        branch = git.run("symbolic-ref", "-q", "HEAD", check=False).stdout.decode().strip() or None
        checks = WorktreeChecks(self.config)
        decision: Decision | None = None
        proposal: Record | None = None
        approved: dict[str, tuple[int, str] | None] | None = None
        session_count = len(audit.view.get("sessions", []))
        saved = primary_error = False
        status = "failed"
        baseline: dict[str, tuple[int, str] | None] | None = None
        try:
            baseline = checks.baseline(git)
            if audit.view.get("interactive"):
                decision, approved = self._review(
                    git, audit, round_number, list(stages), validation, state
                )
            else:
                proposal = audit.add_record(
                    "reviews",
                    {
                        "round": round_number,
                        "attempt": round_number,
                        "status": "running",
                        "artifacts": f"proposals/{round_number:04d}",
                        "feedback": None,
                        "conflict_ids": [record["id"] for record, _ in snapshots],
                    },
                )
                decision = Decision.parse(
                    self.resolver.run(git, audit, round_number, list(stages), validation)
                )
            if decision.confidence < self.config.min_confidence:
                raise LowConfidence("Agent confidence below threshold; worktree preserved")
            checks.guard(git, audit, head, branch)
            checks.verify(git, list(approved) if approved is not None else list(stages), baseline)
            if proposal is not None:
                self._capture_attempt(git, audit, proposal, decision)
                self._capture_changes(git, audit, proposal, list(stages), baseline)
            for record, conflict in snapshots:
                self._save_resolution(git, audit, record, conflict, decision)
            audit.save()
            saved = True
            current = checks.baseline(git)
            selected = sorted(
                set(stages)
                | {
                    name
                    for name in current.keys() | baseline.keys()
                    if current.get(name) != baseline.get(name)
                }
            )
            if approved is None:
                self._step(git, audit, ("add", "-A", "--", *selected))
            else:
                self._stage_approved(git, audit, approved)
            status = "applied"
            for record, _ in snapshots:
                record.update(status=status, application="staged")
            audit.record("reviews").update(status=status, application="staged")
            audit.save()
        except LowConfidence:
            primary_error = True
            status = "low_confidence"
            raise
        except KeyboardInterrupt:
            primary_error = True
            status = "interrupted"
            raise
        except BaseException:
            primary_error = True
            raise
        finally:
            sessions = audit.view.get("sessions", [])
            session = sessions[-1] if len(sessions) > session_count else None
            if decision is None and session is not None and "rationale" in session:
                decision = Decision(session["rationale"], session["confidence"])
            for record, conflict in snapshots:
                record.update(status=status)
                if session is not None:
                    record.artifact("session", f"{session['artifacts']}/messages.json")
                if not saved:
                    error = audit.attempt(
                        "conflict proposal",
                        partial(self._save_resolution, git, audit, record, conflict, decision),
                    )
                    if error is not None:
                        record.update(capture_error=safe_error(error))
            if proposal is not None:
                if not proposal.get("conflicts"):
                    audit.attempt(
                        "attempt proposal",
                        partial(self._capture_attempt, git, audit, proposal, decision),
                    )
                if baseline is not None and not audit.has_artifact(
                    f"{proposal['artifacts']}/files.json"
                ):
                    audit.attempt(
                        "attempt supporting files",
                        partial(
                            self._capture_changes, git, audit, proposal, list(stages), baseline
                        ),
                    )
                proposal.update(status=status, resolution=asdict(decision) if decision else None)
            error = audit.attempt("conflict decisions", audit.save)
            if error is not None and not primary_error:
                raise AuditError("Cannot retain conflict decisions") from error

    def _stage_approved(
        self, git: Git, audit: Audit, approved: dict[str, tuple[int, str] | None]
    ) -> None:
        """Stage the complete reviewed proposal while preserving the caller's index hints."""
        paths = list(approved)
        checks = WorktreeChecks(self.config)
        if checks.fingerprints(git, paths) != approved:
            raise EzError("Approved worktree contents changed before staging")
        assumed, skipped = set(), set()
        for entry in git.run("ls-files", "-v", "-z", "--", *paths).stdout.split(b"\0"):
            if not entry:
                continue
            tag, name = entry[:1].decode("ascii"), entry[2:].decode("utf-8")
            if tag.islower():
                assumed.add(name)
            if tag.upper() == "S":
                skipped.add(name)
        try:
            for flag, names in (("assume-unchanged", assumed), ("skip-worktree", skipped)):
                if names:
                    git.run("update-index", f"--no-{flag}", "--", *sorted(names))
            self._step(git, audit, ("add", "-A", "--", *paths))
            indexed = {}
            for entry in git.run("ls-files", "--stage", "-z", "--", *paths).stdout.split(b"\0"):
                if not entry:
                    continue
                header, raw_name = entry.split(b"\t", 1)
                _, oid, stage = header.decode("ascii").split()
                name = raw_name.decode("utf-8")
                if stage != "0":
                    raise EzError(f"Approved resolution still has unmerged index entries: {name}")
                indexed[name] = oid
            for name, expected in approved.items():
                if expected is None:
                    if name in indexed:
                        raise EzError(f"Approved deletion was not staged: {name}")
                elif (
                    name not in indexed
                    or git.text("hash-object", f"--path={name}", "--", name) != indexed[name]
                ):
                    raise EzError(f"Approved content was not staged: {name}")
            if checks.fingerprints(git, paths) != approved:
                raise EzError("Approved worktree contents changed during staging")
        finally:
            if assumed or skipped:
                remaining = set(
                    git.run("ls-files", "-z", "--", *paths).stdout.decode("utf-8").split("\0")
                )
                for flag, names in (("assume-unchanged", assumed), ("skip-worktree", skipped)):
                    if selected := names & remaining:
                        git.run("update-index", f"--{flag}", "--", *sorted(selected))

    def _review(
        self,
        git: Git,
        audit: Audit,
        round_number: int,
        paths: list[str],
        validation: tuple[str, ...],
        state: ConflictState | None = None,
    ) -> tuple[Decision, dict[str, tuple[int, str] | None]]:
        feedback = None
        head = git.text("rev-parse", "HEAD")
        branch = git.run("symbolic-ref", "-q", "HEAD", check=False).stdout.decode().strip() or None
        checks = WorktreeChecks(self.config)
        before_agent = checks.baseline(git)
        original_index = git.run("ls-files", "--stage", "-z").stdout
        automatic_attempt = 0
        while True:
            if state is not None:
                state.restore()
            automatic_attempt += 1
            if feedback is not None:
                feedback = redact_bytes(
                    feedback.encode(), (self.config.resolved_api_key(),)
                ).decode()
            reviews = audit.view.get("reviews", [])
            number = len(reviews) + 1
            proposal_data: dict[str, Any] = {
                "round": round_number,
                "attempt": number,
                "feedback": feedback,
                "status": "running",
                "application": "not_staged",
                "stop_id": audit.view["conflicts"][-1]["stop_id"],
                "conflict_ids": [record["id"] for record in audit.view["conflicts"][-len(paths) :]],
                "artifacts": f"proposals/{number:04d}",
            }
            proposal = audit.add_record("reviews", proposal_data)
            audit.save()
            directory = audit.directory / proposal["artifacts"]
            primary_error = False
            parsed: Decision | None = None
            session_count = len(audit.view.get("sessions", []))
            try:
                raw = self.resolver.run(git, audit, number, paths, validation)
                parsed = Decision.parse(raw)
                proposal.update(resolution=asdict(parsed))
                checks.guard(git, audit, head, branch)
                checks.verify(git, paths, before_agent)
                validation_failed = False
                diagnostic = None
                if validation:
                    try:
                        self.validate(git, audit, validation)
                    except ChildCleanupError:
                        raise
                    except EzError as exc:
                        validation_failed = True
                        diagnostic = str(exc)
                        output = self._validation_output
                        if output is None:
                            diagnostic += "\nValidation output unavailable."
                        else:
                            for stream in output:
                                diagnostic += "\n" + stream[: self.config.tool_output_bytes].decode(
                                    errors="replace"
                                )
                                if len(stream) > self.config.tool_output_bytes:
                                    diagnostic += "\n[Validation diagnostic truncated]"
                    if diagnostic is not None:
                        diagnostic = redact_bytes(
                            diagnostic.encode(), (self.config.resolved_api_key(),)
                        ).decode()
                    proposal.update(validation=dict(audit.view["validation"]))
                    proposal.update(validation_diagnostic=diagnostic)
                checks.guard(git, audit, head, branch)
                if git.run("ls-files", "--stage", "-z").stdout != original_index:
                    raise EzError("Index changed before approval; integration stopped")
                if state is not None:
                    state.verify_control()
                self._capture_attempt(git, audit, proposal, parsed)
                selected = self._capture_changes(git, audit, proposal, paths, before_agent)
                if state is not None:
                    selected = sorted(set(selected) | set(state.changed_paths()))
                    capture_proposal(audit, git, directory, self.config.max_file_bytes, selected)
                proposal.update(status="proposed")
                audit.save()
                if validation_failed and automatic_attempt < self.config.validation_attempts:
                    proposal.update(status="check_failed", decision="automatic_retry")
                    feedback = f"Previous check failed. Correct the resolution:\n{diagnostic}"
                    continue
                if not validation_failed:
                    checks.verify(git, selected, before_agent)
                reviewed = checks.fingerprints(git, selected)
                reviewed_changes = state.changed_paths() if state is not None else []
                review_baseline = checks.baseline(git)
                index = git.run("ls-files", "--stage", "-z").stdout
                recorded = (
                    read_evidence(directory / "files.json"),
                    read_evidence(directory / "worktree.diff"),
                )
                action, feedback = review_proposal(
                    directory,
                    parsed,
                    (self.config.resolved_api_key(),),
                    validation_failed=validation_failed,
                    diagnostic=diagnostic,
                )
                proposal.update(decision=action)
                proposal.update(user_feedback=feedback)
                audit.save()
                if action == "stop":
                    raise EzError("Review stopped; original unresolved conflict restored")
                checks.guard(git, audit, head, branch)
                if git.run("ls-files", "--stage", "-z").stdout != index:
                    raise EzError("Index changed during review; integration stopped")
                comparison = directory / "after-review"
                capture_proposal(audit, git, comparison, self.config.max_file_bytes, selected)
                if checks.fingerprints(git, selected) != reviewed:
                    raise EzError("Worktree changed during review; review no longer applies")
                if checks.baseline(git) != review_baseline or (
                    state is not None and state.changed_paths() != reviewed_changes
                ):
                    raise EzError("Worktree changed during review; review no longer applies")
                if recorded != (
                    read_evidence(directory / "files.json"),
                    read_evidence(directory / "worktree.diff"),
                ):
                    raise EzError("Proposal evidence changed during review")
                proposed_files = json.loads(read_evidence(directory / "files.json"))["files"]
                reviewed_files = json.loads(read_evidence(comparison / "files.json"))["files"]
                if proposed_files != reviewed_files or (
                    read_evidence(directory / "worktree.diff")
                    != read_evidence(comparison / "worktree.diff")
                ):
                    raise EzError("Worktree changed during review; review no longer applies")
                if state is not None:
                    state.verify_control()
                if action == "accept" and not validation_failed:
                    return parsed, reviewed
                automatic_attempt = 0
            except (EzError, OSError, ValueError, KeyboardInterrupt) as exc:
                primary_error = True
                proposal.update(
                    status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
                )
                proposal.update(error=safe_error(exc))
                raise
            except BaseException as exc:
                primary_error = True
                proposal.update(status="interrupted" if isinstance(exc, SystemExit) else "failed")
                proposal.update(error=f"Unexpected {type(exc).__name__}")
                raise
            finally:
                sessions = audit.view.get("sessions", [])
                if parsed is None and len(sessions) > session_count and "rationale" in sessions[-1]:
                    parsed = Decision(sessions[-1]["rationale"], sessions[-1]["confidence"])
                    proposal.update(resolution=asdict(parsed))
                if not proposal.get("conflicts"):
                    audit.attempt(
                        "attempt proposal",
                        partial(self._capture_attempt, git, audit, proposal, parsed),
                    )
                capture_error = None
                if not audit.has_artifact(f"{proposal['artifacts']}/files.json"):
                    capture_error = audit.attempt(
                        "review worktree",
                        partial(
                            self._capture_changes,
                            git,
                            audit,
                            proposal,
                            paths,
                            before_agent,
                        ),
                        f"{proposal['artifacts']}/files.json",
                    )
                save_error = audit.attempt("review metadata", audit.save)
                if not primary_error and (capture_error is not None or save_error is not None):
                    raise AuditError("Review audit capture failed; inspect audit errors") from (
                        capture_error or save_error
                    )

    def validate(self, git: Git, audit: Audit, command: tuple[str, ...]) -> None:
        self._validation_output: tuple[bytes, bytes] | None = None
        validation = Validation(command=list(command), status="running")
        audit.annotate(validation=validation)
        try:
            audit.save()
        except Exception:
            validation["status"] = "not_run"
            audit.annotate(validation=validation)
            raise
        failure: EzError | None = None
        try:
            runner = run_process if audit.view.get("interactive") else subprocess.run
            arguments: dict[str, Any] = (
                {} if audit.view.get("interactive") else {"capture_output": True, "check": False}
            )
            result = runner(
                command,
                cwd=git.path,
                env=git_environment(),
                timeout=self.config.timeout,
                **arguments,
            )
        except subprocess.TimeoutExpired as exc:
            validation["status"] = "timed_out"
            stdout, stderr = exc.stdout or b"", exc.stderr or b""
            failure = EzError("Validation command timed out")
            failure.__cause__ = exc
        except (OSError, KeyboardInterrupt, ChildCleanupError) as exc:
            validation["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
            audit.annotate(validation=validation)
            raise
        else:
            stdout, stderr = result.stdout, result.stderr
            validation["status"] = "passed" if result.returncode == 0 else "failed"
            validation["returncode"] = result.returncode
            if result.returncode:
                failure = EzError(f"Validation failed with code {result.returncode}")
        self._validation_output = stdout, stderr
        audit.annotate(validation=validation)
        audit.attempt(
            "validation stdout",
            lambda: audit.write("validation.stdout", stdout),
            "validation.stdout",
        )
        audit.attempt(
            "validation stderr",
            lambda: audit.write("validation.stderr", stderr),
            "validation.stderr",
        )
        if failure is not None:
            raise failure
