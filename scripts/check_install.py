# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Verify a built wheel in a fresh environment outside the source checkout."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from functools import partial
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    wheel = args.wheel.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="git-ez-install-") as temporary:
        root = Path(temporary).resolve(strict=True)
        environment = root / "environment"
        subprocess.run(
            [
                "uv",
                "venv",
                "--python",
                sys.executable,
                str(environment),
            ],
            check=True,
            cwd=root,
        )
        python = environment / "bin/python"
        subprocess.run(
            ["uv", "pip", "install", "--python", str(python), str(wheel)],
            check=True,
            cwd=root,
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

        def fails(code: int, *args: str) -> subprocess.CalledProcessError:
            try:
                run(*args)
            except subprocess.CalledProcessError as exc:
                if exc.returncode != code:
                    raise RuntimeError(
                        f"Unexpected exit code for {args}: {exc.returncode}"
                    ) from exc
                return exc
            raise RuntimeError(f"Expected exit {code} for {args}")

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
        run(str(python), "-I", "-c", "import git_ez.agent_runtime; import git_ez.llm")
        run(
            str(python),
            "-I",
            "-c",
            "import importlib.util; "
            "assert importlib.util.find_spec('sqlite_vec') is None; "
            "assert importlib.util.find_spec('langgraph.checkpoint.sqlite') is None",
        )
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
        error = fails(2, "git", "ez", "show", str(report))
        if "Cannot read resolution journal" not in error.stderr:
            raise RuntimeError("Installed extension did not explain the missing manifest")
        (report / "run.json").write_text('{"schema_version":1,"status":"succeeded"}')
        error = fails(2, "git", "ez", "show", str(report))
        if "Cannot read resolution journal" not in error.stderr:
            raise RuntimeError("Installed extension accepted an unsupported audit schema")
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
            if operation == "rebase":
                (repository / "feature.txt").write_text("fixed feature\n", encoding="utf-8")
                git("commit", "-am", "fixup! Feature")
            git("checkout", "main")
            (repository / "main.txt").write_text("main\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "Main")
            git("checkout", "feature")
            options = ("--autosquash",) if operation == "rebase" else ()
            if "Status: succeeded" not in git("ez", operation, "main", *options):
                raise RuntimeError(f"Installed {operation} failed")
            git("merge-base", "--is-ancestor", "main", "HEAD")
            if git("status", "--porcelain"):
                raise RuntimeError(f"Installed {operation} left a dirty checkout")
            audits = list((repository / ".git/ez/runs").iterdir())
            if len(audits) != 1 or "resolution journal" not in git("ez", "show", str(audits[0])):
                raise RuntimeError(f"Installed {operation} did not preserve a readable audit")
            if operation == "rebase":
                if git("rev-list", "--count", "main..HEAD").strip() != "1":
                    raise RuntimeError("Installed autosquash did not group the fixup")
                if (repository / "feature.txt").read_text() != "fixed feature\n":
                    raise RuntimeError("Installed autosquash lost the fixup content")
                if "fixup " not in (audits[0] / "rebase-todo.txt").read_text():
                    raise RuntimeError("Installed autosquash did not capture its sequence")
                if not (audits[0] / "rebase-todo.txt.sha256").is_file():
                    raise RuntimeError("Installed autosquash did not capture its fingerprint")
            manifest_path = audits[0] / "run.json"
            manifest = manifest_path.read_bytes()
            payload = json.loads(manifest)
            if payload["schema_version"] != 4:
                raise RuntimeError("Installed extension did not publish schema 4")
            cache = audits[0] / "report.md"
            cache.write_text("# Stale cached report: failed\n")
            if "Recorded status: **succeeded**" not in run("git", "ez", "show", str(audits[0])):
                raise RuntimeError("Installed reader trusted a stale report")
            cache.unlink()
            run("git", "ez", "show", str(audits[0]))
            if cache.exists() or manifest_path.read_bytes() != manifest:
                raise RuntimeError("Installed reader modified its input")
            if (audits[0] / "result.diff").exists():
                raise RuntimeError("Installed extension duplicated Git history")
            fails(
                1,
                "git",
                "-C",
                str(repository),
                "ez",
                operation,
                "main",
                "--check",
                str(python),
                "-c",
                "raise SystemExit(7)",
            )
            failed = next(
                path for path in (repository / ".git/ez/runs").iterdir() if path != audits[0]
            )
            if "Recorded status: **failed**" not in run("git", "ez", "show", str(failed)):
                raise RuntimeError(
                    "Installed reader could not verify a faithfully recorded failure"
                )
            archived_success = root / f"{operation}-success-audit"
            archived_failure = root / f"{operation}-failed-audit"
            audits[0].rename(archived_success)
            failed.rename(archived_failure)
            shutil.rmtree(repository)
            for archive in (archived_success, archived_failure):
                run("git", "ez", "show", str(archive))
            if (archived_success / "run.json").read_bytes() != manifest:
                raise RuntimeError("Relocated audit reading modified its historical manifest")
        print(
            "Installed wheel: imports, Git discovery, help, manual, audits, "
            "merge, autosquash passed"
        )


if __name__ == "__main__":
    main()
