"""Create a real conflicting repository and run the POC without API credentials."""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from git_ez.config import Config
from git_ez.engine import Engine
from git_ez.git import Git


class DemoResolver:
    """Deterministic fixture for this greeting only; never exposed through Git configuration."""

    def run(self, git, audit, round_number, paths, check):
        if paths != ["greeting.py"]:
            raise ValueError("Unexpected demo conflict")
        (git.path / "greeting.py").write_text(
            'def greet(name):\n    return f"Bonjour, {name.strip()} !"\n'
        )
        return {
            "rationale": "Combine the French greeting and whitespace cleanup.",
            "confidence": 0.99,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ai", action="store_true", help="Use the native ez.* Git configuration")
    parser.add_argument("--operation", choices=("rebase", "merge"), default="rebase")
    args = parser.parse_args()
    directory = Path(tempfile.mkdtemp(prefix="ez-demo-")).resolve()
    subprocess.run(["git", "init", "-b", "main", str(directory)], check=True, capture_output=True)
    git = Git(directory)
    git.run("config", "user.name", "git-ez Demo")
    git.run("config", "user.email", "demo@example.invalid")
    file = directory / "greeting.py"
    file.write_text('def greet(name):\n    return f"Hello, {name}!"\n')
    git.run("add", "greeting.py")
    git.run("commit", "-m", "Initial greeting")
    git.run("checkout", "-b", "feature")
    file.write_text('def greet(name):\n    return f"Hello, {name.strip()}!"\n')
    git.run("commit", "-am", "Trim names")
    git.run("checkout", "main")
    file.write_text('def greet(name):\n    return f"Bonjour, {name} !"\n')
    git.run("commit", "-am", "French greeting")
    config = Config.from_git(directory) if args.ai else Config(model="offline-demo-fixture")
    resolver = None if args.ai else DemoResolver()
    git.run("checkout", "feature")
    audit = Engine(directory, config, resolver).run(
        "main",
        args.operation,
        validation=(
            sys.executable,
            "-B",
            "-c",
            'from greeting import greet; assert greet(" Ada ") == "Bonjour, Ada !"',
        ),
    )
    print(f"Local {args.operation}: {audit.manifest['status']}")
    print(f"Report: {audit.directory / 'report.md'}")
    if audit.manifest["status"] != "succeeded":
        raise SystemExit(audit.manifest.get("error", "Demo failed"))
    print(f"Demo repository (kept for inspection): {directory}")


if __name__ == "__main__":
    main()
