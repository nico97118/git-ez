# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Build a target-specific, offline-installable wheelhouse from uv.lock."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from email.parser import BytesParser
from pathlib import Path


def wheel_record(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        metadata = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata) != 1:
            raise ValueError(f"Expected one distribution metadata file in {path.name}")
        message = BytesParser().parsebytes(archive.read(metadata[0]))
    name = message["Name"]
    version = message["Version"]
    if not name or not version:
        raise ValueError(f"Missing name or version in {path.name}")
    return {
        "name": re.sub(r"[-_.]+", "-", name).lower(),
        "version": version,
        "file": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "license": message["License-Expression"] or message["License"] or "Not declared",
    }


def write_manifest(root: Path, project_wheel: Path, target: str) -> None:
    records = [wheel_record(path) for path in sorted((root / "wheels").glob("*.whl"))]
    names = [item["name"] for item in records]
    if len(names) != len(set(names)):
        raise ValueError("Wheelhouse contains multiple wheels for the same distribution")
    project = wheel_record(project_wheel)
    if project["name"] != "git-ez-ai" or project not in records:
        raise ValueError("Wheelhouse does not contain the exact git-ez-ai wheel")
    (root / "requirements.txt").write_text(
        "".join(
            f"{item['name']}=={item['version']} --hash=sha256:{item['sha256']}\n"
            for item in records
        )
    )
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "target": target,
                "python": sys.version,
                "distributions": records,
            },
            indent=2,
        )
        + "\n"
    )
    (root / "README.txt").write_text(
        f"git-ez {project['version']} wheelhouse for {target}\n\n"
        "Requires the matching system/architecture/CPython minor version, Git >= 2.38, "
        "and uv. Python and system shared libraries are not bundled.\n"
        "FreeBSD requires system Python with its SQLite module; see freebsd.md.\n"
        "Install from this extracted directory:\n\n"
        "  uv tool install --offline --no-index --no-build --no-cache "
        "--find-links ./wheels "
        f"--constraint ./requirements.txt git-ez-ai=={project['version']}\n\n"
        "A compatible Python must already be installed. The provider must be configured "
        "separately. A remote provider still requires network access at runtime.\n"
        "requirements.txt pins the bundled versions and records wheel hashes. "
        "manifest.json records the build target and declared licenses. "
        "Third-party licenses and notices remain inside their wheels. "
        "License declarations are not a complete redistribution review.\n"
    )


def build(wheel: Path, output: Path, target: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", target):
        raise ValueError("Target must be a lowercase archive-safe label")
    wheel = wheel.resolve(strict=True)
    output.mkdir(parents=True, exist_ok=True)
    project = wheel_record(wheel)
    with tempfile.TemporaryDirectory(prefix="git-ez-wheelhouse-") as temporary:
        temporary_root = Path(temporary)
        root = temporary_root / f"git-ez-{project['version']}-{target}"
        wheels = root / "wheels"
        wheels.mkdir(parents=True)
        source_requirements = root / "requirements-source.txt"
        subprocess.run(
            [
                "uv",
                "export",
                "--locked",
                "--no-default-groups",
                "--no-emit-project",
                "--output-file",
                str(source_requirements),
            ],
            check=True,
        )
        # pip's wheel command can collect downloaded and locally built wheels;
        # uv has no wheelhouse command. Keep this build-only tool outside the project.
        env = dict(
            os.environ,
            UV_CACHE_DIR=str(temporary_root / "uv-cache"),
            PIP_CACHE_DIR=str(temporary_root / "pip-cache"),
            PIP_DISABLE_PIP_VERSION_CHECK="1",
        )
        subprocess.run(
            [
                "uv",
                "run",
                "--no-project",
                "--python",
                sys.executable,
                "--with",
                "pip==25.3",
                "python",
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--require-hashes",
                "--requirement",
                str(source_requirements),
                "--wheel-dir",
                str(wheels),
            ],
            check=True,
            env=env,
        )
        shutil.copy2(wheel, wheels / wheel.name)
        shutil.copy2("uv.lock", root / "uv.lock")
        for name in ("LICENSE", "docs/licensing.md", "docs/freebsd.md"):
            shutil.copy2(name, root / Path(name).name)
        write_manifest(root, wheel, target)
        archive = output / f"{root.name}.tar.gz"
        with tarfile.open(archive, "w:gz") as stream:
            stream.add(root, arcname=root.name)
    return archive


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    print(build(args.wheel, args.output, args.target))


if __name__ == "__main__":
    main()
