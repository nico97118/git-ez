# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Release requests and approved asset inventories reject inconsistent inputs."""

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from scripts import release, release_assets, wheelhouse


@pytest.mark.unit
@pytest.mark.parametrize("value", ["1.2", "../1.0.0", "01.2.3", "1.2.3+local", "1.2.3;echo bad"])
def test_release_version_requires_canonical_public_version(value):
    with pytest.raises(ValueError, match="canonical"):
        release.version_key(value)


@pytest.mark.unit
def test_prereleases_sort_before_their_final_release():
    versions = ["0.2.0a1", "0.2.0b1", "0.2.0rc1", "0.2.0rc2", "0.2.0", "0.2.1"]
    assert sorted(reversed(versions), key=release.version_key) == versions


@pytest.fixture
def request_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".github").mkdir()
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    (tmp_path / "uv.lock").write_text('[[package]]\nname = "git-ez-ai"\nversion = "0.1.0"\n')
    release.REQUEST.write_text(json.dumps({"version": "0.1.0", "wheelhouses": "none"}))
    release.NOTES.write_text("Initial release\n")
    return tmp_path


@pytest.mark.unit
@pytest.mark.parametrize("file", ["pyproject.toml", "uv.lock"])
def test_request_rejects_version_disagreement(request_directory, file):
    path = Path(file)
    path.write_text(path.read_text().replace("0.1.0", "0.2.0"))
    with pytest.raises(ValueError, match="version"):
        release.read_request()


@pytest.mark.unit
def test_request_rejects_unknown_targets(request_directory):
    release.REQUEST.write_text(json.dumps({"version": "0.1.0", "wheelhouses": "windows"}))
    with pytest.raises(ValueError, match="selection"):
        release.read_request()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("bump", "explicit", "expected"),
    [
        ("current", "", "0.1.0"),
        ("patch", "", "0.1.1"),
        ("minor", "", "0.2.0"),
        ("explicit", "0.2.0rc1", "0.2.0rc1"),
    ],
)
def test_preparation_uses_uv_and_records_the_same_version_in_the_request(
    repo, monkeypatch, bump, explicit, expected
):
    monkeypatch.chdir(repo.root)
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.setenv("UV_PYTHON", sys.executable)
    monkeypatch.setenv("UV_CACHE_DIR", str(repo.root / ".uv-cache"))
    Path(".github").mkdir()
    Path("pyproject.toml").write_text(
        '[project]\nname = "git-ez-ai"\nversion = "0.1.0"\n'
        'requires-python = ">=3.11"\ndependencies = []\n'
    )
    subprocess.run(["uv", "lock", "--offline"], check=True)
    result = subprocess.run(
        [
            sys.executable,
            str(Path(release.__file__).resolve()),
            "prepare",
            "--bump",
            bump,
            "--version",
            explicit,
            "--wheelhouses",
            "freebsd",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.splitlines() == [
        f"version={expected}",
        "wheelhouses=freebsd",
        f"prerelease={str('rc' in expected).lower()}",
    ]
    assert release.read_request() == {"version": expected, "wheelhouses": "freebsd"}
    assert release.NOTES.read_text().startswith(f"# git-ez {expected}\n")
    assert repo.git.run("tag", "--list").stdout == b""


@pytest.mark.integration
def test_preparation_refuses_to_reuse_an_existing_release_tag(repo, monkeypatch):
    monkeypatch.chdir(repo.root)
    Path(".github").mkdir()
    Path("pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    Path("uv.lock").write_text('[[package]]\nname = "git-ez-ai"\nversion = "0.1.0"\n')
    repo.git.run("tag", "v0.1.0")
    with pytest.raises(ValueError, match="existing tag"):
        release.prepare("current", "", "none")


@pytest.fixture
def assets(tmp_path):
    root = tmp_path / "assets"
    root.mkdir()
    for name in release_assets.asset_names("0.1.0", "freebsd"):
        (root / name).write_bytes(name.encode())
    return root


@pytest.mark.unit
def test_approved_assets_verify_and_detect_tampering(assets):
    release_assets.assemble(assets, "0.1.0", "freebsd", "a" * 40)
    assert len(release_assets.verify(assets, "0.1.0", "freebsd", "a" * 40)) == 5
    (assets / "git_ez_ai-0.1.0-py3-none-any.whl").write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        release_assets.verify(assets, "0.1.0", "freebsd", "a" * 40)


@pytest.mark.unit
def test_missing_wheelhouse_prevents_assembly(assets):
    next(assets.glob("git-ez-*.tar.gz")).unlink()
    with pytest.raises(ValueError, match="inventory"):
        release_assets.assemble(assets, "0.1.0", "freebsd", "a" * 40)


@pytest.mark.unit
def test_approved_assets_cannot_be_published_for_another_commit(assets):
    release_assets.assemble(assets, "0.1.0", "freebsd", "a" * 40)
    with pytest.raises(ValueError, match="approved commit"):
        release_assets.verify(assets, "0.1.0", "freebsd", "b" * 40)


@pytest.mark.unit
def test_assets_reject_symlinks(assets, tmp_path):
    wheel = assets / "git_ez_ai-0.1.0-py3-none-any.whl"
    wheel.unlink()
    target = tmp_path / "outside.whl"
    target.write_bytes(b"external")
    wheel.symlink_to(target)
    with pytest.raises(ValueError, match="regular file"):
        release_assets.assemble(assets, "0.1.0", "freebsd", "a" * 40)


@pytest.mark.unit
def test_wheelhouse_manifest_pins_built_wheel_hashes(tmp_path):
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    for name, version in [("git_ez_ai", "0.1.0"), ("native_dep", "2.0")]:
        path = wheels / f"{name}-{version}-py3-none-any.whl"
        with zipfile.ZipFile(path, "w") as stream:
            stream.writestr(
                f"{name}-{version}.dist-info/METADATA",
                f"Name: {name}\nVersion: {version}\nLicense-Expression: MIT\n",
            )
    project = wheels / "git_ez_ai-0.1.0-py3-none-any.whl"
    wheelhouse.write_manifest(tmp_path, project, "freebsd15.1-amd64-py312")
    requirements = (tmp_path / "requirements.txt").read_text()
    assert "git-ez-ai==0.1.0 --hash=sha256:" in requirements
    assert "native-dep==2.0 --hash=sha256:" in requirements
    assert len(json.loads((tmp_path / "manifest.json").read_text())["distributions"]) == 2
    with zipfile.ZipFile(wheels / "native_dep-3.0-py3-none-any.whl", "w") as stream:
        stream.writestr("native_dep-3.0.dist-info/METADATA", "Name: native_dep\nVersion: 3.0\n")
    with pytest.raises(ValueError, match="multiple wheels"):
        wheelhouse.write_manifest(tmp_path, project, "freebsd15.1-amd64-py312")


@pytest.mark.unit
def test_public_release_is_not_overwritten(assets, monkeypatch, mocker):
    release_assets.assemble(assets, "0.1.0", "freebsd", "a" * 40)
    monkeypatch.setenv("REDISTRIBUTION_APPROVED", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    command = mocker.patch(
        "scripts.release_assets.subprocess.run",
        return_value=subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                [
                    [
                        {
                            "tag_name": "v0.1.0",
                            "draft": False,
                        }
                    ]
                ]
            ),
        ),
    )
    with pytest.raises(ValueError, match="already public"):
        release_assets.publish(assets, "0.1.0", "freebsd", "a" * 40)
    assert command.call_count == 1


@pytest.mark.unit
def test_wheelhouse_publication_requires_the_redistribution_review(assets, monkeypatch, mocker):
    monkeypatch.delenv("REDISTRIBUTION_APPROVED", raising=False)
    command = mocker.patch("scripts.release_assets.subprocess.run")
    with pytest.raises(ValueError, match="redistribution review"):
        release_assets.publish(assets, "0.1.0", "freebsd", "a" * 40)
    command.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("resume", [False, True])
def test_publication_creates_or_resumes_only_a_matching_draft(assets, monkeypatch, mocker, resume):
    sha = "a" * 40
    release_assets.assemble(assets, "0.1.0", "freebsd", sha)
    files = release_assets.verify(assets, "0.1.0", "freebsd", sha)
    monkeypatch.setenv("REDISTRIBUTION_APPROVED", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    draft = {"tag_name": "v0.1.0", "draft": True, "target_commitish": sha}
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if "--slurp" in command:
            payload = [[draft]] if resume else [[]]
        elif "git/matching-refs" in command[2]:
            payload = [{"ref": "refs/tags/v0.1.0", "object": {"sha": sha}}] if resume else []
        elif command[1] == "api" and command[2].endswith("/git/refs"):
            assert json.loads(kwargs["input"]) == {"ref": "refs/tags/v0.1.0", "sha": sha}
            payload = {}
        elif command[1] == "api" and "/releases/tags/" in command[2]:
            payload = dict(
                draft,
                assets=[
                    {
                        "name": path.name,
                        "size": path.stat().st_size,
                        "digest": "sha256:"
                        + release_assets.hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                    for path in files
                ],
            )
        else:
            payload = {}
        return subprocess.CompletedProcess(command, 0, json.dumps(payload))

    mocker.patch("scripts.release_assets.subprocess.run", side_effect=run)
    release_assets.publish(assets, "0.1.0", "freebsd", sha)
    creations = [call for call in calls if call[:3] == ["gh", "release", "create"]]
    assert len(creations) == (0 if resume else 1)
    assert calls[-1][:3] == ["gh", "release", "edit"]
    assert "--draft=false" in calls[-1]
    upload = next(call for call in calls if call[:3] == ["gh", "release", "upload"])
    assert all(str(path) in upload for path in files)


@pytest.mark.unit
def test_an_older_approval_cannot_replace_a_newer_stable_release(assets, monkeypatch, mocker):
    release_assets.assemble(assets, "0.1.0", "freebsd", "a" * 40)
    monkeypatch.setenv("REDISTRIBUTION_APPROVED", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    command = mocker.patch(
        "scripts.release_assets.subprocess.run",
        return_value=subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                [
                    [
                        {
                            "tag_name": "v0.2.0",
                            "draft": False,
                            "prerelease": False,
                        }
                    ]
                ]
            ),
        ),
    )
    with pytest.raises(ValueError, match="newer stable release"):
        release_assets.publish(assets, "0.1.0", "freebsd", "a" * 40)
    assert command.call_count == 1


@pytest.mark.unit
def test_github_api_failures_keep_the_reason_and_exception_cause(mocker):
    failure = subprocess.CalledProcessError(1, ["gh", "api"], stderr="Permission denied")
    mocker.patch("scripts.release_assets.subprocess.run", side_effect=failure)
    with pytest.raises(RuntimeError, match="Permission denied") as error:
        release_assets.api("repos/owner/repo/releases")
    assert error.value.__cause__ is failure
