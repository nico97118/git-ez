# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Local rebase/merge, audit consultation and model compatibility commands."""

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .config import Config
from .engine import Engine
from .errors import EzError


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="git ez", description="Local Git rebase/merge with traceable AI conflict resolution"
    )
    root.add_argument("--version", action="version", version=f"git-ez {__version__}")
    commands = root.add_subparsers(dest="command", required=True)
    for operation in ("rebase", "merge"):
        run = commands.add_parser(
            operation, help=f"Run a local {operation} with AI conflict resolution"
        )
        run.add_argument("target", help="Git revision to integrate into the current branch")
        if operation == "merge":
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
        config = Config.from_git(args.repo)
        engine = Engine(args.repo, config)
        audit = engine.run(
            args.target,
            args.command,
            validation=tuple(args.check or ()),
            merge_ff=getattr(args, "merge_ff", None),
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
