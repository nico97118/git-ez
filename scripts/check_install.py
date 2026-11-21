# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Verify a built wheel in a fresh environment outside the source checkout."""

import os
import re
import subprocess
import sys
import tempfile
from functools import partial
from pathlib import Path


def main() -> None:
    wheel = Path(sys.argv[1]).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="git-ez-install-") as temporary:
        root = Path(temporary).resolve(strict=True)
        environment = root / "environment"
        subprocess.run(
            ["uv", "venv", "--python", sys.executable, str(environment)], check=True, cwd=root
        )
        python = environment / "bin/python"
        subprocess.run(
            ["uv", "pip", "install", "--python", str(python), str(wheel)], check=True, cwd=root
        )
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        env.update(
            PATH=str(environment / "bin") + os.pathsep + env.get("PATH", ""),
            MANPATH=str(environment / "share/man") + os.pathsep,
            MANPAGER="cat",
            GIT_PAGER="cat",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
        )

        def run(*args: str) -> str:
            result = subprocess.run(
                args, cwd=root, env=env, check=True, capture_output=True, text=True
            )
            return re.sub(r".\x08", "", result.stdout)

        distribution = run(
            str(python),
            "-I",
            "-c",
            "from importlib.metadata import metadata; print(metadata('git-ez-ai')['Name'])",
        ).strip()
        if distribution != "git-ez-ai":
            raise RuntimeError(f"Unexpected installed distribution: {distribution}")
        license_expression = run(
            str(python),
            "-I",
            "-c",
            "from importlib.metadata import metadata; "
            "print(metadata('git-ez-ai')['License-Expression'])",
        ).strip()
        if license_expression != "GPL-3.0-only":
            raise RuntimeError(f"Unexpected installed license: {license_expression}")
        installed = Path(
            run(str(python), "-I", "-c", "import git_ez; print(git_ez.__file__)").strip()
        )
        if not installed.resolve(strict=True).is_relative_to(environment.resolve(strict=True)):
            raise RuntimeError(f"Imported code outside the installed environment: {installed}")
        if not run("git", "ez", "--version").startswith("git-ez "):
            raise RuntimeError("Installed Git extension did not report its version")
        for args in (("ez", "-h"), ("ez", "rebase", "-h")):
            if "usage:" not in run("git", *args):
                raise RuntimeError(f"Missing command-line help: {args}")
        for args in (("ez", "--help"), ("help", "ez")):
            if "SYNOPSIS" not in run("git", *args):
                raise RuntimeError(f"Missing Git manual: {args}")
        report = root / "audit"
        report.mkdir()
        (report / "report.md").write_text("# Installed wheel audit\n", encoding="utf-8")
        if "# Installed wheel audit" not in run("git", "ez", "show", str(report)):
            raise RuntimeError("Installed extension could not display an audit")
        for operation in ("merge", "rebase"):
            repository = root / operation
            run("git", "init", "-b", "main", str(repository))

            git = partial(run, "git", "-C", str(repository))

            git("config", "user.name", "Installed wheel test")
            git("config", "user.email", "wheel@example.invalid")
            git("config", "ez.model", "unused-no-conflicts")
            git("config", "ez.endpoint", "http://127.0.0.1:1/v1")
            (repository / "base.txt").write_text("base\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "Base")
            git("checkout", "-b", "feature")
            (repository / "feature.txt").write_text("feature\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "Feature")
            git("checkout", "main")
            (repository / "main.txt").write_text("main\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "Main")
            git("checkout", "feature")
            if "Status: succeeded" not in git("ez", operation, "main"):
                raise RuntimeError(f"Installed {operation} failed")
            git("merge-base", "--is-ancestor", "main", "HEAD")
            if git("status", "--porcelain"):
                raise RuntimeError(f"Installed {operation} left a dirty checkout")
            audits = list((repository / ".git/ez/runs").iterdir())
            if len(audits) != 1 or "integration report" not in git("ez", "show", str(audits[0])):
                raise RuntimeError(f"Installed {operation} did not preserve a readable audit")
        print("Installed wheel: imports, Git discovery, help, manual, audits, merge, rebase passed")


if __name__ == "__main__":
    main()
