"""Verify a built wheel in a fresh environment outside the source checkout."""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    wheel = Path(sys.argv[1]).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="git-ez-install-") as temporary:
        root = Path(temporary)
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

        installed = Path(
            run(str(python), "-I", "-c", "import git_ez; print(git_ez.__file__)").strip()
        )
        if not installed.is_relative_to(environment):
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
        print("Installed wheel: imports, Git discovery, help, manual, and audit display passed")


if __name__ == "__main__":
    main()
