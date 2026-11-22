# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Local Git rebase/merge with traceable AI conflict resolution."""

import difflib
import hashlib
import json
import subprocess
from collections.abc import Callable
from dataclasses import asdict
from functools import partial
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from .agent import AGENT_PROMPT, AgentResolver, Resolver
from .audit import Audit, Record
from .audit_state import Validation, now, safe_error
from .audit_storage import read_regular as read_evidence
from .checks import WorktreeChecks
from .config import Config
from .conflicts import Conflict, Decision, LowConfidence
from .errors import AuditError, EzError
from .git import Git, Stage, git_environment
from .locking import checkout_lock
from .options import CommitOptions, RebaseOptions
from .review import review_proposal
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
                audit.attempt(
                    "configuration export",
                    lambda: audit.write_json(
                        "config.json", self.config.audit_dict(), optional=True
                    ),
                    "config.json",
                    optional=True,
                )
                audit.attempt(
                    "prompt export",
                    lambda: audit.write("system-prompt.txt", AGENT_PROMPT.encode(), optional=True),
                    "system-prompt.txt",
                    optional=True,
                )
                phase = "integration"
                self._integrate(audit, validation)
                phase = "result_capture"
                self._record_result(audit)
                if validation:
                    phase = "validation"
                    self.validate(git, audit, validation)
                phase = "verification"
                try:
                    self._verify_result(audit)
                except BaseException as exc:
                    audit.checked(exc)
                    raise
                audit.checked()
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
                result_artifacts = ["result.diff"]
                if operation == "rebase":
                    result_artifacts.append("range-diff.txt")
                if audit.view["integrated"] and any(
                    not audit.has_artifact(path) for path in result_artifacts
                ):
                    audit.attempt("integration result", partial(self._record_result, audit))
                audit.expect("exit/files.json", "final_inventory")
                capture_error = audit.attempt(
                    "exit worktree",
                    lambda: audit.capture_worktree(
                        git,
                        audit.directory / "exit",
                        self.config.max_file_bytes,
                        reference=audit.view.get("result_oid", audit.view["before_oid"]),
                    ),
                    "exit/files.json",
                )
                if audit.has_artifact("exit/files.json"):
                    audit.annotate(exit_state="exit/files.json")
                if capture_error is not None:
                    audit.annotate(capture_error="Final worktree evidence is incomplete")
                    if audit.view["status"] == "succeeded":
                        audit.set_status(
                            "failed", AuditError("Final audit capture failed; inspect the report")
                        )
                if "rebase_sequence" in audit.view:
                    sequence_error = audit.attempt(
                        "rebase sequence",
                        lambda: self._verify_sequence_capture(audit),
                        audit.view["rebase_sequence"]["artifact"],
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
                raw = self.git.run("cat-file", "commit", selection).stdout
                artifact = "rebase-selection-anchor.txt"
                audit.write(artifact, raw[: self.config.max_file_bytes])
                plan["selection_anchor"] = {
                    "oid": selection,
                    "parents": [upstream, fork],
                    "tree_oid": tree,
                    "artifact": artifact,
                    "command": ["git", *args],
                    "truncated": len(raw) > self.config.max_file_bytes,
                    "limit_bytes": self.config.max_file_bytes,
                }
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
                "schema_version": 2,
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
            configuration=self.config.audit_dict(),
            prompt=AGENT_PROMPT,
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
        on_success: Callable[[str], None] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        index = audit.begin_step(git, command)
        try:
            result = git.run(*command, check=False, sequence_editor=sequence_editor)
        except BaseException as exc:
            audit.step_unknown(index, exc)
            raise
        observation_ok = audit.returned(git, index, result.returncode)
        if result.returncode == 0 and on_success is not None:
            on_success(audit.step_id(index))
        errors = []
        for channel, content in (("stdout", result.stdout), ("stderr", result.stderr)):
            artifact = f"git-steps/{index + 1:04d}.{channel}"
            errors.append(
                audit.attempt(
                    f"Git {channel}",
                    partial(audit.write, artifact, content),
                    artifact,
                )
            )
        audit.trace(command, result.stdout + result.stderr)
        audit.save()
        if not observation_ok:
            raise AuditError(
                "Cannot observe Git state after native command; completion is uncertain"
            )
        if any(error is not None for error in errors):
            raise AuditError("Native Git output evidence could not be retained")
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
            *args: str, sequence_editor: str | None = None
        ) -> subprocess.CompletedProcess[bytes]:
            nonlocal auto_resolved
            previous_tree = git.run(
                "rev-parse", "--verify", "AUTO_MERGE^{tree}", check=False
            ).stdout
            command = (*rerere_config, *args)
            result = self._step(git, audit, command, sequence_editor=sequence_editor)
            conflict_tree = git.run(
                "rev-parse", "--verify", "AUTO_MERGE^{tree}", check=False
            ).stdout
            # A new conflict tree distinguishes rerere from a failing hook/commit on
            # an already resolved stop. Never retry a rejected hook implicitly.
            auto_resolved = rerere and bool(conflict_tree) and conflict_tree != previous_tree
            return result

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
                    "status": "pending",
                    "limit_bytes": self.config.max_file_bytes,
                }
                audit.annotate(rebase_sequence=sequence_evidence)
                audit.save()
                sequence_editor = editor_command(sequence, self.config.max_file_bytes)
                # Prevent a configured multiline instruction format from adding
                # instructions to the internally generated interactive sequence.
                prefix = ("-c", "rebase.instructionFormat=%s")
            try:
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
                            raise ValueError("Captured sequence changed before Git returned")
                    except (OSError, ValueError) as exc:
                        evidence.update(status="unavailable", error=str(exc))
                    else:
                        evidence.update(
                            status="captured",
                            sha256=digest,
                            size_bytes=len(content),
                        )
                    audit.annotate(rebase_sequence=evidence)
                    for artifact in (evidence["artifact"], evidence["digest_artifact"]):
                        if evidence["status"] == "captured":
                            audit.register_external(artifact)
                    audit.save()
        else:
            # Let Git fast-forward or create a merge commit, as git merge normally would.
            merge_ff = audit.view["merge_ff"]
            flags = (f"--{merge_ff}",) if merge_ff is not None else ()
            result = step(
                "merge", "--no-edit", *flags, *commit_options.flags(), audit.view["target_oid"]
            )
        rounds = 0
        while result.returncode:
            stages = git.conflicts()
            if not stages:
                if not auto_resolved or not git.operation_active():
                    raise EzError("Git stopped without resolvable conflicts; see integration.log")
                audit.add_record(
                    "native_resolutions",
                    {
                        "mechanism": "rerere",
                        "head": git.text("rev-parse", "HEAD"),
                        "rationale": "Git reused and staged a recorded conflict resolution.",
                    },
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
                        raise EzError("Cannot retain empty commit; see integration.log")
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

    def _record_result(self, audit: Audit) -> None:
        git = self.git
        before_oid = audit.view["before_oid"]
        result_oid = audit.view.get("result_oid") or git.text("rev-parse", "HEAD")
        audit.result(result_oid)
        errors = []
        if not audit.has_artifact("result.diff"):
            errors.append(
                audit.attempt(
                    "result diff",
                    lambda: audit.write(
                        "result.diff",
                        git.run(
                            "diff",
                            "--no-ext-diff",
                            "--no-textconv",
                            "--binary",
                            before_oid,
                            result_oid,
                        ).stdout,
                    ),
                    "result.diff",
                )
            )
        if audit.operation == "rebase" and not audit.has_artifact("range-diff.txt"):
            errors.append(
                audit.attempt("result comparison", partial(self._record_rebase_result, audit))
            )
        audit.save()
        if any(error is not None for error in errors):
            raise AuditError("Integration result evidence could not be retained") from next(
                error for error in errors if error is not None
            )

    def _record_rebase_result(self, audit: Audit) -> None:
        git = self.git
        before_oid = audit.inputs.before_oid
        result_oid = audit.view["result_oid"]
        if audit.view["operation"] == "rebase":
            plan = audit.view["rebase_plan"]
            selection_oid = plan["selection_oid"]
            base_oid = plan["base_oid"]
            ranges = (
                (f"{selection_oid}..{before_oid}", f"{base_oid}..{result_oid}")
                if selection_oid != base_oid
                else (selection_oid, before_oid, result_oid)
            )
            # Git rejects empty ranges in its two-range syntax. Represent an empty
            # side at the other base using the three-revision syntax instead.
            if selection_oid != base_oid:
                if not git.text("rev-list", f"{selection_oid}..{before_oid}"):
                    ranges = (base_oid, base_oid, result_oid)
                elif not git.text("rev-list", f"{base_oid}..{result_oid}"):
                    ranges = (selection_oid, before_oid, selection_oid)
            args = (
                "range-diff",
                "--no-color",
                "--no-ext-diff",
                "--no-textconv",
                *ranges,
            )
            audit.write("range-diff.txt", git.run(*args).stdout)
            comparison = {
                "artifact": "range-diff.txt",
                "before": f"{selection_oid}..{before_oid}",
                "after": f"{base_oid}..{result_oid}",
                "command": ["git", *args],
            }
            audit.annotate(range_diff=comparison)
            if "rebase_sequence" in audit.view:
                contents = git.run(
                    "log",
                    "--reverse",
                    "--no-color",
                    "--no-decorate",
                    "--no-show-signature",
                    "--no-notes",
                    "--format=raw",
                    f"{base_oid}..{result_oid}",
                ).stdout
                artifact = "rebase-commits.log"
                audit.write(artifact, contents[: self.config.max_file_bytes])
                result_commits = {
                    "artifact": artifact,
                    "truncated": len(contents) > self.config.max_file_bytes,
                    "limit_bytes": self.config.max_file_bytes,
                }
                audit.annotate(result_commits=result_commits)
        audit.save()

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
    ) -> tuple[Record, Conflict]:
        number = len(audit.view["conflicts"]) + 1
        record_data: dict[str, Any] = {
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
        record_handle = audit.add_record("conflicts", record_data)
        audit.save()
        record = record_handle
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
            record.artifact(label, audit.artifact(number, f"{label}.txt", decoded))
        marked = None
        if path.exists():
            if path.stat().st_size > (10_000_000):
                raise EzError(f"Marked file too large: {name}")
            try:
                raw_marked = read_evidence(path, 10_000_001)
                if len(raw_marked) > 10_000_000:
                    raise EzError(f"Marked file too large: {name}")
                marked = raw_marked.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise EzError(f"Non-UTF-8 working file requires manual resolution: {name}") from exc
            record.artifact("marked", audit.artifact(number, "marked.txt", marked))
        context = git.text("log", "-5", "--format=%h %s", "HEAD")
        if record["replay_commit"]:
            context += "\nReplaying: " + git.text("show", "-s", "--format=%h %s", "REBASE_HEAD")
        conflict = Conflict(
            name,
            audit.view["operation"],
            contents["base"],
            contents["ours"],
            contents["theirs"],
            marked,
            context,
        )
        request_artifact = audit.artifact(
            number,
            "request.json",
            json.dumps(conflict.payload(), indent=2, ensure_ascii=True),
        )
        record.artifact("request", request_artifact)
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
        primary_error = False
        session = audit.directory / "sessions" / f"{round_number:04d}"
        head = git.text("rev-parse", "HEAD")
        branch = git.run("symbolic-ref", "-q", "HEAD", check=False).stdout.decode().strip() or None
        checks = WorktreeChecks(self.config)
        try:
            baseline = checks.baseline(git)
            if audit.view.get("interactive"):
                parsed, session = self._review(git, audit, round_number, list(stages), validation)
            else:
                automatic = audit.add_record(
                    "reviews",
                    {
                        "attempt": round_number,
                        "round": round_number,
                        "status": "running",
                        "artifacts": f"proposals/auto-{round_number:04d}",
                        "mode": "automatic",
                        "feedback": None,
                    },
                )
                audit.save()
                raw = self.resolver.run(git, audit, round_number, list(stages), validation)
                parsed = Decision.parse(raw)
                automatic.update(status="proposed", resolution=asdict(parsed))
                audit.capture_worktree(
                    git,
                    audit.directory / automatic["artifacts"],
                    self.config.max_file_bytes,
                    list(stages),
                )
            decision = asdict(parsed)
            if parsed.confidence < self.config.min_confidence:
                raise LowConfidence("Agent confidence below threshold; worktree preserved")
            checks.guard(git, audit, head, branch)
            checks.verify(git, list(stages), baseline)

            # Only the orchestrator stages a verified result, including supporting files.
            staged_artifact = str(session.relative_to(audit.directory) / "staged.diff")

            def staged(step_id: str) -> None:
                nonlocal status
                status = "applied"
                audit.expect(staged_artifact, "staged_diff")
                for record, _ in snapshots:
                    record.update(status="applied", application="staged", stage_step_id=step_id)
                    record.artifact("staged", staged_artifact)
                if audit.view.get("interactive"):
                    audit.record("reviews").update(status="applied", application="staged")

            result = self._step(git, audit, ("add", "-A", "--", "."), on_success=staged)
            if result.returncode:
                raise EzError("Git staging failed; inspect native step output")
            audit.create_directory(session)
            audit.write(
                staged_artifact,
                git.run(
                    "diff", "--cached", "--no-ext-diff", "--no-textconv", "--binary", "HEAD"
                ).stdout,
            )
            status = "applied"
            if audit.view.get("interactive"):
                audit.record("reviews").update(status="applied", application="staged")
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
            session_number: int | None = round_number
            if audit.view.get("interactive"):
                # A failed review never returns its session; use only this stop's attempt.
                review = next(
                    (
                        review
                        for review in reversed(audit.view.get("reviews", []))
                        if review["round"] == round_number
                    ),
                    None,
                )
                session_number = review["attempt"] if review else None
            final_session = (
                audit.directory / "sessions" / f"{session_number:04d}"
                if session_number is not None
                else None
            )
            sessions = audit.view.get("sessions", [])
            details = sessions[-1] if sessions and sessions[-1]["round"] == session_number else {}
            if not decision and "rationale" in details:
                decision = {"rationale": details["rationale"], "confidence": details["confidence"]}
            if details.get("status") == "low_confidence":
                status = "low_confidence"
            if not audit.view.get("interactive") and audit.view.get("reviews"):
                audit.record("reviews").update(
                    status=status, application="staged" if status == "applied" else "not_staged"
                )
            for record, conflict in snapshots:
                record.update(status=status)
                for label, filename in (("session", "messages.json"), ("files", "files.json")):
                    if final_session is not None and audit.has_artifact(
                        (final_session / filename).relative_to(audit.directory).as_posix()
                    ):
                        record.artifact(
                            label, str((final_session / filename).relative_to(audit.directory))
                        )
                try:
                    path = git.safe_path(conflict.path)
                    if path.exists() and path.stat().st_size > self.config.max_file_bytes:
                        raise EzError("Proposal exceeds size limit; see session file snapshot")
                    content = (
                        read_evidence(path, self.config.max_file_bytes + 1).decode("utf-8")
                        if path.exists()
                        else None
                    )
                    if decision:
                        resolution = {
                            "action": "write" if content is not None else "delete",
                            "content": content,
                            **decision,
                        }
                    if decision:
                        record.update(resolution=resolution)
                    proposed_artifact = audit.artifact(
                        record["number"], "proposed.txt", content or ""
                    )
                    record.artifact("proposed", proposed_artifact)
                    diff = "".join(
                        difflib.unified_diff(
                            (conflict.ours or "").splitlines(keepends=True),
                            (content or "").splitlines(keepends=True),
                            fromfile="ours",
                            tofile="proposed",
                        )
                    )
                    diff_artifact = audit.artifact(record["number"], "resolution.diff", diff)
                    record.artifact("diff", diff_artifact)
                except (EzError, OSError, ValueError) as exc:
                    record.update(capture_error=safe_error(exc))
                    audit.capture_error("proposal evidence", exc, None)
            save_error = audit.attempt("conflict metadata", audit.save)
            if save_error is not None and not primary_error:
                raise EzError("Conflict audit capture failed; inspect audit errors") from save_error

    def _review(
        self,
        git: Git,
        audit: Audit,
        round_number: int,
        paths: list[str],
        validation: tuple[str, ...],
    ) -> tuple[Decision, Path]:
        feedback = None
        head = git.text("rev-parse", "HEAD")
        branch = git.run("symbolic-ref", "-q", "HEAD", check=False).stdout.decode().strip() or None
        checks = WorktreeChecks(self.config)
        while True:
            reviews = audit.view.get("reviews", [])
            number = len(reviews) + 1
            proposal_data: dict[str, Any] = {
                "round": round_number,
                "attempt": number,
                "feedback": feedback,
                "status": "running",
                "artifacts": f"proposals/{number:04d}",
            }
            proposal = audit.add_record("reviews", proposal_data)
            audit.save()
            directory = audit.directory / proposal["artifacts"]
            primary_error = False
            try:
                raw = self.resolver.run(git, audit, number, paths, validation)
                parsed = Decision.parse(raw)
                proposal.update(resolution=asdict(parsed))
                audit.capture_worktree(git, directory, self.config.max_file_bytes, paths)
                proposal.update(status="proposed")
                audit.save()
                review_baseline = checks.baseline(git)
                index = git.run("ls-files", "--stage", "-z").stdout
                action, feedback = review_proposal(directory, parsed)
                proposal.update(decision=action)
                proposal.update(user_feedback=feedback)
                audit.save()
                if action == "stop":
                    raise EzError("Review stopped; proposed files and Git operation preserved")
                checks.guard(git, audit, head, branch)
                if git.run("ls-files", "--stage", "-z").stdout != index:
                    raise EzError("Index changed during review; integration stopped")
                comparison = directory / "after-review"
                audit.capture_worktree(git, comparison, self.config.max_file_bytes, paths)
                if checks.baseline(git) != review_baseline:
                    raise EzError("Worktree changed during review; review no longer applies")
                for evidence_path in (directory / "files.json", directory / "worktree.diff"):
                    if not audit.has_artifact(
                        evidence_path.relative_to(audit.directory).as_posix()
                    ):
                        raise EzError("Proposal evidence changed during review")
                proposed_files = json.loads(read_evidence(directory / "files.json"))["files"]
                reviewed_files = json.loads(read_evidence(comparison / "files.json"))["files"]
                if proposed_files != reviewed_files or (
                    read_evidence(directory / "worktree.diff")
                    != read_evidence(comparison / "worktree.diff")
                ):
                    raise EzError("Worktree changed during review; review no longer applies")
                if action == "accept":
                    return parsed, audit.directory / "sessions" / f"{number:04d}"
            except (EzError, OSError, ValueError, KeyboardInterrupt) as exc:
                primary_error = True
                proposal.update(
                    status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
                )
                proposal.update(error=str(exc) or "Interrupted by user")
                raise
            except BaseException as exc:
                primary_error = True
                proposal.update(status="interrupted" if isinstance(exc, SystemExit) else "failed")
                proposal.update(error=f"Unexpected {type(exc).__name__}")
                raise
            finally:
                capture_error = None
                if not audit.has_artifact(f"{proposal['artifacts']}/files.json"):
                    capture_error = audit.attempt(
                        "review worktree",
                        partial(
                            audit.capture_worktree,
                            git,
                            directory,
                            self.config.max_file_bytes,
                            paths,
                        ),
                        f"{proposal['artifacts']}/files.json",
                    )
                save_error = audit.attempt("review metadata", audit.save)
                if not primary_error and (capture_error is not None or save_error is not None):
                    raise EzError("Review audit capture failed; inspect audit errors") from (
                        capture_error or save_error
                    )

    def validate(self, git: Git, audit: Audit, command: tuple[str, ...]) -> None:
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
            result = subprocess.run(
                command,
                cwd=git.path,
                env=git_environment(),
                capture_output=True,
                timeout=self.config.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            validation["status"] = "timed_out"
            stdout, stderr = exc.stdout or b"", exc.stderr or b""
            failure = EzError("Validation command timed out")
            failure.__cause__ = exc
        except (OSError, KeyboardInterrupt) as exc:
            validation["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
            for name in ("validation.stdout", "validation.stderr"):
                audit.unavailable(name, "Validation did not return output", optional=True)
            audit.annotate(validation=validation)
            raise
        else:
            stdout, stderr = result.stdout, result.stderr
            validation["status"] = "passed" if result.returncode == 0 else "failed"
            validation["returncode"] = result.returncode
            if result.returncode:
                failure = EzError(f"Validation failed with code {result.returncode}")
        audit.annotate(validation=validation)
        output_error = audit.attempt(
            "validation stdout",
            lambda: audit.write("validation.stdout", stdout),
            "validation.stdout",
        )
        error_output_error = audit.attempt(
            "validation stderr",
            lambda: audit.write("validation.stderr", stderr),
            "validation.stderr",
        )
        if failure is not None:
            raise failure
        if output_error is not None or error_output_error is not None:
            raise EzError("Validation audit capture failed; inspect audit errors") from (
                output_error or error_output_error
            )
