# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Local rebase/merge, audit consultation and model compatibility commands."""

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any, TypeVar, overload

from . import __version__
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
            if argument in ("--gpg-sign", "-S"):
                arguments[index] = "--gpg-sign="
        parsed = super().parse_args(arguments, namespace)
        if validation is not None:
            if getattr(parsed, "command", None) not in ("rebase", "merge"):
                self.error("--check is only supported for rebase and merge")
            parsed.check = validation
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
        run.add_argument("target", help="Git revision to integrate into the current branch")
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
    show = commands.add_parser("show", help="Display a saved integration report")
    show.add_argument("run_directory", type=Path, help="Audit directory printed after execution")
    probe = commands.add_parser("probe", help="Test model/tool compatibility without touching Git")
    probe.add_argument("--output", type=Path, required=True, help="Local JSON diagnostic report")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "show":
            print((args.run_directory / "report.md").read_text(encoding="utf-8"))
            return 0
        if args.command == "probe":
            from .probe import probe

            report = probe(Config.from_git(Path.cwd()), args.output)
            print(f"Compatibility: {report['status']}")
            print(f"Report: {args.output.resolve()}")
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
        )
        config = Config.from_git(args.repo)
        engine = Engine(args.repo, config)
        audit = engine.run(
            args.target,
            args.command,
            validation=tuple(args.check or ()),
            merge_ff=getattr(args, "merge_ff", None),
            onto=getattr(args, "onto", None),
            rebase_options=(
                RebaseOptions(
                    args.empty, args.keep_empty, args.reapply_cherry_picks, args.force_rebase
                )
                if args.command == "rebase"
                else None
            ),
            commit_options=commit_options,
            rerere_autoupdate=args.rerere_autoupdate,
        )
        print(f"Status: {audit.manifest['status']}")
        print(f"Report: {audit.directory / 'report.md'}")
        print(f"Branch: {audit.manifest['branch']}")
        print(f"Operation: {audit.manifest['operation']}")
        print(f"Integrated: {audit.manifest['integrated']}")
        if audit.manifest.get("error"):
            print(f"Error: {audit.manifest['error']}", file=sys.stderr)
        return 1 if audit.manifest["status"] in ("failed", "interrupted") else 0
    except (EzError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"git-ez: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
