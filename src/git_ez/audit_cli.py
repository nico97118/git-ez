# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Locate local journals and display saved reports using Git pager conventions."""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .audit import load_report
from .audit_report import safe_text
from .audit_storage import read_regular
from .errors import EzError
from .git import Git


def local_runs() -> list[tuple[Path, dict[str, Any]]]:
    common = Path(Git(Path.cwd()).text("rev-parse", "--path-format=absolute", "--git-common-dir"))
    root = common / "ez/runs"
    runs = []
    if root.is_dir():
        for directory in root.iterdir():
            if directory.is_symlink() or not directory.is_dir():
                continue
            try:
                data = json.loads(read_regular(directory / "run.json"))
                if (
                    not isinstance(data, dict)
                    or data.get("schema_version") != 4
                    or data["id"] != directory.name
                ):
                    raise ValueError("Unsupported journal")
                # Validate the fields used for discovery without opening supporting captures.
                for key in ("created_at", "operation", "branch", "status"):
                    if not isinstance(data[key], str):
                        raise ValueError("Invalid journal summary")
                runs.append((directory, data))
            except (OSError, ValueError, KeyError, TypeError):
                print(safe_text(f"Warning: unreadable journal: {directory}"), file=sys.stderr)
    return sorted(runs, key=lambda run: (run[1]["created_at"], run[0].name), reverse=True)


def consult(action: str, run_id: str | None, path: Path | None, verbose: bool) -> str:
    if path is not None:
        return load_report(path.resolve(), verbose=verbose)
    runs = local_runs()
    if action == "list":
        lines = ["ID  Started  Operation  Branch  Status  Checkout"]
        for directory, data in runs:
            lines.append(
                "  ".join(
                    safe_text(str(v))
                    for v in (
                        directory.name,
                        data["created_at"],
                        f"{data['operation']} {data.get('target', '')}",
                        data["branch"],
                        data["status"],
                        data.get("worktree", "unavailable"),
                    )
                )
            )
        return "\n".join(lines if runs else ["No audit runs found."]) + "\n"
    matches = [
        directory for directory, _ in runs if run_id is None or directory.name.startswith(run_id)
    ]
    if not matches:
        raise EzError("No matching audit run found")
    if run_id is not None and len(matches) > 1:
        raise EzError("Ambiguous audit run identifier; use a longer prefix")
    return load_report(matches[0], verbose=verbose)


def display(text: str) -> None:
    if not sys.stdout.isatty() or os.environ.get("GIT_PAGER_IN_USE"):
        print(text, end="")
        return
    git = Git(Path.cwd())
    configured = git.run("config", "--get", "pager.ez", check=False).stdout.decode().strip()
    disabled = configured.lower() in ("false", "no", "off", "0")
    if disabled and "GIT_PAGER" not in os.environ:
        print(text, end="")
        return
    pager = os.environ.get("GIT_PAGER")
    if pager is None:
        pager = (
            configured
            if configured and configured.lower() not in ("true", "yes", "on", "1")
            else git.run("var", "GIT_PAGER", check=False).stdout.decode().strip()
        )
    if not pager or pager == "cat":
        print(text, end="")
        return
    words = shlex.split(pager)
    if not words or (len(words) == 1 and shutil.which(words[0]) is None):
        print(text, end="")
        return
    environment = dict(os.environ)
    environment.setdefault("LESS", "FRX")
    with subprocess.Popen(
        pager, shell=True, stdin=subprocess.PIPE, text=True, env=environment
    ) as process:
        try:
            process.communicate(text)
        except BrokenPipeError:
            process.wait()
    if process.returncode in (126, 127):
        # The configured shell command could not start the pager.
        print(text, end="")
