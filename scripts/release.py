# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Prepare and validate the versioned request used by the release workflows."""

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

REQUEST = Path(".github/release-request.json")
NOTES = Path(".github/release-notes.md")
VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:(a|b|rc)(0|[1-9]\d*))?")


def version_key(value: str) -> tuple[int, int, int, int, int]:
    match = VERSION.fullmatch(value)
    if match is None:
        raise ValueError("Use a canonical X.Y.Z version, optionally followed by aN, bN or rcN")
    major, minor, patch, stage, number = match.groups()
    return (
        int(major),
        int(minor),
        int(patch),
        {"a": 0, "b": 1, "rc": 2, None: 3}[stage],
        int(number or 0),
    )


def project_version() -> str:
    with Path("pyproject.toml").open("rb") as stream:
        value: str = tomllib.load(stream)["project"]["version"]
    version_key(value)
    return value


def read_request() -> dict[str, Any]:
    request: dict[str, Any] = json.loads(REQUEST.read_text())
    if set(request) != {"version", "wheelhouses"}:
        raise ValueError("Release request must contain exactly version and wheelhouses")
    if request["version"] != project_version():
        raise ValueError("Release request version does not match pyproject.toml")
    if request["wheelhouses"] not in ("none", "freebsd", "all"):
        raise ValueError("Unknown wheelhouse selection")
    with Path("uv.lock").open("rb") as stream:
        lock = tomllib.load(stream)
    packages = [item for item in lock["package"] if item["name"] == "git-ez-ai"]
    if len(packages) != 1 or packages[0]["version"] != request["version"]:
        raise ValueError("Release version does not match uv.lock")
    if not NOTES.read_text().strip():
        raise ValueError("Release notes must not be empty")
    return request


def prepare(bump: str, explicit: str, wheelhouses: str) -> None:
    previous = project_version()
    if bump == "explicit":
        if version_key(explicit) <= version_key(previous):
            raise ValueError("Explicit release version must be newer than the project version")
        command = ["uv", "version", explicit, "--no-sync"]
    elif bump == "current":
        if explicit:
            raise ValueError("An explicit version requires bump=explicit")
        command = []
    else:
        if explicit:
            raise ValueError("An explicit version requires bump=explicit")
        command = ["uv", "version", "--bump", bump, "--no-sync"]
    with Path("uv.lock").open("rb") as stream:
        before = tomllib.load(stream)
    if command:
        subprocess.run(command, check=True, stdout=sys.stderr)
    current = project_version()
    with Path("uv.lock").open("rb") as stream:
        after = tomllib.load(stream)
    # A release bump must not silently upgrade or otherwise change dependencies.
    for lock in (before, after):
        for package in lock["package"]:
            if package["name"] == "git-ez-ai":
                package["version"] = "<release>"
    if before != after:
        raise ValueError(
            "Version preparation changed the dependency lock beyond the project version"
        )
    REQUEST.write_text(
        json.dumps({"version": current, "wheelhouses": wheelhouses}, indent=2) + "\n"
    )
    tags = subprocess.run(
        ["git", "tag", "--merged", "HEAD", "--list", "v*"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    valid = [tag for tag in tags if VERSION.fullmatch(tag[1:])]
    previous_tag = max(valid, key=lambda tag: version_key(tag[1:])) if valid else None
    if previous_tag and version_key(current) <= version_key(previous_tag[1:]):
        raise ValueError(f"Version must be newer than existing tag {previous_tag}")
    revisions = [f"{previous_tag}..HEAD"] if previous_tag else ["HEAD"]
    changes = subprocess.run(
        ["git", "log", "--format=- %s (%h)", *revisions],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    NOTES.write_text(f"# git-ez {current}\n\n{changes or 'Initial release.'}\n")
    read_request()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument(
        "--bump", choices=("current", "patch", "minor", "major", "explicit"), required=True
    )
    prepare_parser.add_argument("--version", default="")
    prepare_parser.add_argument("--wheelhouses", choices=("none", "freebsd", "all"), default="none")
    commands.add_parser("plan")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.bump, args.version, args.wheelhouses)
    request = read_request()
    print(f"version={request['version']}")
    print(f"wheelhouses={request['wheelhouses']}")
    print(f"prerelease={str(VERSION.fullmatch(request['version']).group(4) is not None).lower()}")


if __name__ == "__main__":
    main()
