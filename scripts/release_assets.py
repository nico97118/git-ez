# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Assemble and publish only the exact files approved by the release workflow."""

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from scripts.release import NOTES, VERSION, read_request, version_key

TARGETS = {
    "none": (),
    "freebsd": ("freebsd15.1-amd64-py312",),
    "all": ("freebsd15.1-amd64-py312", "linux-glibc2.39-x86_64-py312", "macos15-arm64-py312"),
}


def asset_names(version: str, wheelhouses: str) -> set[str]:
    return {
        f"git_ez_ai-{version}-py3-none-any.whl",
        f"git_ez_ai-{version}.tar.gz",
        *(f"git-ez-{version}-{target}.tar.gz" for target in TARGETS[wheelhouses]),
    }


def checksums(root: Path, expected: set[str]) -> str:
    if {item.name for item in root.iterdir()} != expected:
        raise ValueError(f"Release asset inventory differs from expected files: {sorted(expected)}")
    lines = []
    for name in sorted(expected):
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Release asset must be a regular file: {name}")
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {name}\n")
    return "".join(lines)


def assemble(root: Path, version: str, wheelhouses: str, sha: str) -> None:
    digest = checksums(root, asset_names(version, wheelhouses))
    (root / "SHA256SUMS").write_text(digest)
    (root / "release.json").write_text(
        json.dumps(
            {
                "version": version,
                "commit": sha,
                "wheelhouses": wheelhouses,
                "sha256sums": digest,
            },
            indent=2,
        )
        + "\n"
    )


def verify(root: Path, version: str, wheelhouses: str, sha: str) -> list[Path]:
    expected = asset_names(version, wheelhouses)
    for name in ("release.json", "SHA256SUMS"):
        if (root / name).is_symlink() or not (root / name).is_file():
            raise ValueError(f"Missing regular release metadata file: {name}")
    metadata = json.loads((root / "release.json").read_text())
    if metadata != {
        "version": version,
        "commit": sha,
        "wheelhouses": wheelhouses,
        "sha256sums": (root / "SHA256SUMS").read_text(),
    }:
        raise ValueError("Release metadata does not match the approved commit and request")
    all_names = expected | {"release.json", "SHA256SUMS"}
    # Verify the complete inventory, then compare only payload checksums.
    actual = checksums(root, all_names)
    payload = "".join(
        line
        for line in actual.splitlines(keepends=True)
        if line.split("  ", 1)[1].strip() in expected
    )
    if payload != metadata["sha256sums"]:
        raise ValueError("Release asset checksum mismatch")
    return [root / name for name in sorted(all_names)]


def api(endpoint: str, payload: dict[str, Any] | None = None, *, paginate: bool = False) -> Any:
    command = ["gh", "api", endpoint]
    if payload is not None:
        command.extend(["--input", "-"])
    if paginate:
        command.extend(["--paginate", "--slurp"])
    try:
        result = subprocess.run(
            command,
            input=json.dumps(payload) if payload is not None else None,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"GitHub API request failed for {endpoint}: {exc.stderr.strip()}"
        ) from exc
    return json.loads(result.stdout)


def publish(root: Path, version: str, wheelhouses: str, sha: str) -> None:
    if wheelhouses != "none" and os.environ.get("REDISTRIBUTION_APPROVED") != "true":
        raise ValueError("Wheelhouse redistribution review has not been enabled")
    files = verify(root, version, wheelhouses, sha)
    repository = os.environ["GITHUB_REPOSITORY"]
    tag = f"v{version}"
    # Serialize publication in Actions; never replace a public release.
    published = [
        item for page in api(f"repos/{repository}/releases", paginate=True) for item in page
    ]
    releases = [item for item in published if item["tag_name"] == tag]
    if releases and (len(releases) != 1 or not releases[0]["draft"]):
        raise ValueError(f"Release {tag} is already public; refusing to overwrite it")
    if releases and releases[0]["target_commitish"] != sha:
        raise ValueError("Existing draft belongs to a different commit")
    for item in published:
        other = item["tag_name"].removeprefix("v")
        if (
            not item["draft"]
            and VERSION.fullmatch(other)
            and not item.get("prerelease", False)
            and version_key(other) > version_key(version)
        ):
            raise ValueError(f"A newer stable release already exists: {item['tag_name']}")
    refs = api(f"repos/{repository}/git/matching-refs/tags/{tag}")
    refs = [item for item in refs if item["ref"] == f"refs/tags/{tag}"]
    if refs:
        if len(refs) != 1 or refs[0]["object"]["sha"] != sha:
            raise ValueError("Existing tag does not point to the tested commit")
    else:
        api(f"repos/{repository}/git/refs", {"ref": f"refs/tags/{tag}", "sha": sha})
    if not releases:
        command = [
            "gh",
            "release",
            "create",
            tag,
            "--repo",
            repository,
            "--verify-tag",
            "--target",
            sha,
            "--title",
            tag,
            "--notes-file",
            str(NOTES),
            "--draft",
        ]
        if re.search(r"(a|b|rc)\d+$", version):
            command.append("--prerelease")
        subprocess.run(command, check=True)
    # A partial upload can be retried, but only while the release remains a draft.
    subprocess.run(
        [
            "gh",
            "release",
            "upload",
            tag,
            "--repo",
            repository,
            "--clobber",
            *(str(path) for path in files),
        ],
        check=True,
    )
    draft = api(f"repos/{repository}/releases/tags/{tag}")
    if not draft["draft"]:
        raise ValueError("Release was published concurrently before upload verification")
    uploaded = {item["name"]: item for item in draft["assets"]}
    if set(uploaded) != {path.name for path in files}:
        raise ValueError("Draft assets differ from the approved inventory")
    for path in files:
        asset = uploaded[path.name]
        if asset["size"] != path.stat().st_size:
            raise ValueError(f"Uploaded size differs for {path.name}")
        digest = asset.get("digest")
        if digest and digest != f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}":
            raise ValueError(f"Uploaded digest differs for {path.name}")
    subprocess.run(
        [
            "gh",
            "release",
            "edit",
            tag,
            "--repo",
            repository,
            "--notes-file",
            str(NOTES),
            "--draft=false",
            "--prerelease=" + str(bool(re.search(r"(a|b|rc)\d+$", version))).lower(),
            "--latest=" + str(not re.search(r"(a|b|rc)\d+$", version)).lower(),
        ],
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("assemble", "publish"))
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    request = read_request()
    version = os.environ["VERSION"]
    wheelhouses = os.environ["WHEELHOUSES"]
    sha = os.environ["RELEASE_SHA"]
    if request != {"version": version, "wheelhouses": wheelhouses}:
        raise ValueError("Workflow outputs do not match the checked-out release request")
    if re.fullmatch("[0-9a-f]{40}", sha) is None:
        raise ValueError("Expected a full commit SHA")
    if args.command == "assemble":
        assemble(args.directory, version, wheelhouses, sha)
    else:
        publish(args.directory, version, wheelhouses, sha)


if __name__ == "__main__":
    main()
