# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Local rebase/merge, audit consultation and model compatibility commands."""

import argparse
import json
import shlex
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any, TypeVar, overload

from . import __version__
from .audit_report import safe_text
from .audit_safety import redact_bytes
from .config import Config
from .engine import Engine
from .errors import EzError
from .options import CommitOptions, RebaseOptions

NamespaceT = TypeVar("NamespaceT")


class GitArgumentParser(argparse.ArgumentParser):
    @overload
    def parse_args(
        self, args: Iterable[str] | None = None, namespace: None = None
    ) -> argparse.Namespace: ...

    @overload
    def parse_args(self, args: Iterable[str] | None, namespace: NamespaceT) -> NamespaceT: ...

    @overload
    def parse_args(self, *, namespace: NamespaceT) -> NamespaceT: ...

    def parse_args(self, args: Iterable[str] | None = None, namespace: Any = None) -> Any:
        """Preserve Git-style signing keys and literal validation arguments."""
        arguments = list(sys.argv[1:] if args is None else args)
        validation: list[str] | None = None
        for index, argument in enumerate(arguments):
            if argument == "--":
                break
            option, separator, value = argument.partition("=")
            if option.startswith("--") and "--check".startswith(option):
                # Detach the command before argparse can interpret its flags or
                # literal --, including with abbreviated and attached forms.
                validation = ([value] if separator else []) + arguments[index + 1 :]
                arguments = arguments[:index]
                break
            if argument == "--log":
                arguments[index] = "--log=20"
            if argument in ("--gpg-sign", "-S"):
                arguments[index] = "--gpg-sign="
        parsed = super().parse_args(arguments, namespace)
        if validation is not None:
            if getattr(parsed, "command", None) not in ("rebase", "merge"):
                self.error("--check is only supported for rebase and merge")
            parsed.check = validation
        if getattr(parsed, "command", None) in ("rebase", "merge"):
            continuing = parsed.continue_operation
            if continuing and parsed.target is not None:
                self.error("--continue cannot be combined with a target")
            if not continuing and parsed.target is None:
                self.error("a target or --continue is required")
            if continuing:
                allowed = {
                    "command",
                    "target",
                    "continue_operation",
                    "interactive",
                    "repo",
                    "check",
                }
                for name, value in vars(parsed).items():
                    if name not in allowed and value is not None:
                        self.error(f"--continue cannot be combined with --{name.replace('_', '-')}")
        return parsed


def parser() -> argparse.ArgumentParser:
    root = GitArgumentParser(
        prog="git ez", description="Local Git rebase/merge with traceable AI conflict resolution"
    )
    root.add_argument("--version", action="version", version=f"git-ez {__version__}")
    commands = root.add_subparsers(dest="command", required=True)
    for operation in ("rebase", "merge"):
        run = commands.add_parser(
            operation, help=f"Run a local {operation} with AI conflict resolution"
        )
        run.add_argument(
            "--interactive", action="store_true", help="Review each agent proposal before staging"
        )
        run.add_argument(
            "target", nargs="?", help="Git revision to integrate into the current branch"
        )
        run.add_argument(
            "--continue",
            dest="continue_operation",
            action="store_true",
            help="Let Git continue manual work; assist only at a new rebase conflict",
        )
        run.add_argument(
            "--gpg-sign",
            "-S",
            nargs="?",
            const="",
            default=None,
            metavar="KEY",
            help="Sign new commits; attach an optional key with --gpg-sign=KEY or -SKEY",
        )
        run.add_argument("--no-gpg-sign", dest="gpg_sign", action="store_const", const=False)
        run.add_argument("--signoff", action=argparse.BooleanOptionalAction, default=None)
        run.add_argument(
            "--rerere-autoupdate",
            action=argparse.BooleanOptionalAction,
            default=None,
            help="Allow Git rerere to stage reused resolutions; overrides Git configuration",
        )
        if operation == "rebase":
            run.add_argument("--onto", help="Replay commits after target onto this revision")
            run.add_argument(
                "--keep-base",
                action=argparse.BooleanOptionalAction,
                default=None,
                help="Replay on the unique common ancestor instead of the upstream tip",
            )
            run.add_argument(
                "--fork-point",
                action=argparse.BooleanOptionalAction,
                default=None,
                help="Use the upstream reflog to select commits; overrides rebase.forkPoint",
            )
            run.add_argument(
                "--autosquash",
                action=argparse.BooleanOptionalAction,
                default=None,
                help="Group fixup!/squash!/amend! commits without opening an editor",
            )
            run.add_argument("--empty", choices=("drop", "keep", "stop"))
            run.add_argument("--keep-empty", action=argparse.BooleanOptionalAction, default=None)
            run.add_argument(
                "--reapply-cherry-picks", action=argparse.BooleanOptionalAction, default=None
            )
            run.add_argument(
                "--force-rebase",
                "-f",
                "--no-ff",
                dest="force_rebase",
                action="store_const",
                const=True,
                default=None,
                help="Replay commits even when unchanged",
            )
            run.add_argument("--ff", dest="force_rebase", action="store_const", const=False)
        if operation == "merge":
            run.add_argument(
                "--log",
                type=int,
                default=None,
                metavar="N",
                help="Include up to N commit subjects; bare --log uses 20",
            )
            run.add_argument("--no-log", dest="log", action="store_const", const=0)
            run.add_argument(
                "--cleanup", choices=("strip", "whitespace", "verbatim", "scissors", "default")
            )
            messages = run.add_mutually_exclusive_group()
            messages.add_argument(
                "--message", "-m", action="append", help="Merge message paragraph"
            )
            messages.add_argument(
                "--file", "-F", type=Path, help="Read the merge message from a file"
            )
            run.add_argument(
                "--verify-signatures", action=argparse.BooleanOptionalAction, default=None
            )
            for flag, help_text in (
                ("ff", "Allow fast-forward; otherwise create a merge commit"),
                ("no-ff", "Create a merge commit when integration is needed"),
                ("ff-only", "Refuse integration unless fast-forward is possible"),
            ):
                run.add_argument(
                    f"--{flag}",
                    dest="merge_ff",
                    action="store_const",
                    const=flag,
                    help=help_text + "; the last policy specified wins",
                )
        run.add_argument(
            "--repo", type=Path, default=Path("."), help="Repository (default: current directory)"
        )
        run.add_argument(
            "--check",
            nargs=argparse.REMAINDER,
            help="Final validation executable and arguments; must be the last option",
        )
    audit = commands.add_parser("audit", help="Inspect local resolution journals")
    audit_commands = audit.add_subparsers(dest="audit_command")
    audit_commands.add_parser("list", help="List saved runs, newest first")
    show = audit_commands.add_parser("show", help="Display the latest or selected run")
    show.add_argument("run_id", nargs="?", help="Run identifier or unique prefix")
    show.add_argument("--path", type=Path, help="Read a copied run directory")
    show.add_argument("--verbose", action="store_true", help="Include model/tool events and output")
    commands.add_parser(
        "probe",
        help="Test model/tool compatibility without touching Git",
        description=(
            "Write the final JSON compatibility report to stdout; diagnostics use stderr. "
            "Save it with: git ez probe > report.json"
        ),
    )
    return root


def _continuation_hint(engine: Engine, args: argparse.Namespace) -> None:
    active = engine.git.rebase_active() if args.command == "rebase" else engine.git.merge_active()
    if not active:
        return
    command = ["git", "ez", args.command, "--continue"]
    if args.interactive:
        command.append("--interactive")
    if args.repo != Path("."):
        command.extend(("--repo", str(args.repo)))
    if args.check:
        command.extend(("--check", *args.check))
    # Native continuation may be interrupted before model configuration is loaded.
    key = (
        engine.git.run("config", "--get", "ez.apiKey", check=False)
        .stdout.decode()
        .removesuffix("\n")
    )
    key_env = (
        engine.git.run("config", "--get", "ez.apiKeyEnv", check=False)
        .stdout.decode()
        .removesuffix("\n")
    )
    credentials = Config(api_key=key or None, api_key_env=key_env or "OPENAI_API_KEY")
    secrets = (engine.config.resolved_api_key(), credentials.resolved_api_key())
    command = [redact_bytes(argument.encode(), secrets).decode() for argument in command]
    print(
        f"{args.command.capitalize()} interrupted.\n\n"
        "Resolve the current conflict manually and stage the resolution, then resume with:\n\n"
        f"  {safe_text(shlex.join(command))}\n\nOr use native Git in the repository:\n\n"
        f"  git {args.command} --continue",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "audit":
            from .audit_cli import consult, display

            if args.audit_command is None:
                parser().parse_args(["audit", "--help"])
            if getattr(args, "path", None) is not None and args.run_id is not None:
                raise EzError("--path and a run identifier are mutually exclusive")
            display(
                consult(
                    args.audit_command,
                    getattr(args, "run_id", None),
                    getattr(args, "path", None),
                    getattr(args, "verbose", False),
                )
            )
            return 0
        if args.command == "probe":
            from .probe import probe

            report = probe(Config.from_git(Path.cwd()))
            print(json.dumps(report, indent=2, ensure_ascii=True), flush=True)
            print(f"Compatibility: {report['status']}", file=sys.stderr)
            return 0 if report["status"] == "compatible" else 1
        if args.check == []:
            raise EzError("--check requires an executable")
        message = None
        if args.command == "merge":
            if args.file is not None:
                if args.file == Path("-"):
                    raise EzError("--file requires a message file; standard input is unsupported")
                message = args.file.read_text(encoding="utf-8")
            elif args.message is not None:
                message = "\n\n".join(args.message)
        commit_options = CommitOptions(
            message=message,
            gpg_sign=args.gpg_sign,
            signoff=args.signoff,
            verify_signatures=getattr(args, "verify_signatures", None),
            log=getattr(args, "log", None),
            cleanup=getattr(args, "cleanup", None),
        )
        config = None if args.continue_operation else Config.from_git(args.repo)
        engine = Engine(args.repo, config)
        if args.continue_operation:
            continued = engine.continue_operation(
                args.command, tuple(args.check or ()), interactive=args.interactive
            )
            if isinstance(continued, int):
                return continued
            audit = continued
        else:
            audit = engine.run(
                args.target,
                args.command,
                validation=tuple(args.check or ()),
                merge_ff=getattr(args, "merge_ff", None),
                onto=getattr(args, "onto", None),
                rebase_options=(
                    RebaseOptions(
                        empty=args.empty,
                        keep_empty=args.keep_empty,
                        reapply_cherry_picks=args.reapply_cherry_picks,
                        force_rebase=args.force_rebase,
                        autosquash=args.autosquash,
                        keep_base=bool(args.keep_base),
                        fork_point=args.fork_point,
                    )
                    if args.command == "rebase"
                    else None
                ),
                commit_options=commit_options,
                rerere_autoupdate=args.rerere_autoupdate,
                interactive=args.interactive,
            )
        print(f"Audit: {safe_text(audit.directory.name)}")
        print(f"Inspect: git ez audit show {safe_text(audit.directory.name)}")
        print(f"Status: {audit.view['status']}")
        if audit.owned:
            print(f"Report: {safe_text(str(audit.directory / 'report.md'))}")
        else:
            print(
                "Intended audit directory (not created by this run): "
                f"{safe_text(str(audit.directory))}"
            )
        print(f"Branch: {safe_text(audit.view['branch'])}")
        print(f"Operation: {audit.view['operation']}")
        print(f"Integrated: {audit.view['integrated']}")
        if audit.view.get("error"):
            print(f"Error: {safe_text(audit.view['error'])}", file=sys.stderr)
        for error in audit.audit_errors:
            print(safe_text(f"Audit error ({error['phase']}): {error['message']}"), file=sys.stderr)
        for warning in audit.presentation_errors:
            print(f"Warning: {safe_text(warning)}", file=sys.stderr)
        if not audit.published:
            print(
                "Resolution journal could not be saved; inspect the retained files.",
                file=sys.stderr,
            )
        if audit.view["status"] == "interrupted":
            _continuation_hint(engine, args)
            if args.interactive:
                return 130
        return 0 if audit.view["status"] == "succeeded" and audit.published else 1
    except KeyboardInterrupt:
        if "engine" in locals():
            _continuation_hint(engine, args)
        return 130
    except (EzError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"git-ez: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
