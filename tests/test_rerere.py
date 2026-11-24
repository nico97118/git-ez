# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Native resolution reuse and explicit rerere policy overrides."""

import pytest

from git_ez.options import CommitOptions
from tests.helpers import integrate

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("operation", ["rebase", "merge", "cherry-pick"])
def test_rerere_can_reuse_a_resolution_without_calling_the_provider(repo, operation):
    repo.git.run("config", "rerere.enabled", "true")
    repo.git.run("config", "rerere.autoupdate", "true")
    first = integrate(repo, operation)
    assert first.view["status"] == "succeeded", first.view
    repo.git.run("reset", "--hard", repo.source_oid)
    repo.resolver.calls.clear()
    second = integrate(repo, operation)
    assert second.view["status"] == "succeeded", second.view
    assert repo.resolver.calls == []


def test_rerere_does_not_retry_a_rejected_commit_hook(repo):
    repo.git.run("config", "rerere.enabled", "true")
    repo.git.run("config", "rerere.autoupdate", "true")
    assert integrate(repo, "merge").view["status"] == "succeeded"
    repo.git.run("reset", "--hard", repo.source_oid)
    hook = repo.root / ".git/hooks/pre-commit"
    count = repo.root / ".git/hook-count"
    hook.write_text("#!/bin/sh\necho called >> .git/hook-count\nexit 1\n")
    hook.chmod(0o755)
    audit = integrate(repo, "merge")
    assert audit.view["status"] == "failed", audit.view
    assert count.read_text().splitlines() == ["called"]


@pytest.mark.parametrize("operation", ["rebase", "merge"])
@pytest.mark.parametrize("autoupdate", [True, False])
def test_explicit_rerere_policy_overrides_configuration_without_changing_it(
    repo, operation, autoupdate
):
    repo.git.run("config", "rerere.enabled", "true")
    configured = "false" if autoupdate else "true"
    repo.git.run("config", "rerere.autoupdate", configured)
    first = integrate(repo, operation, rerere_autoupdate=True)
    assert first.view["status"] == "succeeded", first.view
    repo.git.run("reset", "--hard", repo.source_oid)
    repo.resolver.calls.clear()
    second = integrate(repo, operation, rerere_autoupdate=autoupdate)
    assert second.view["status"] == "succeeded", second.view
    assert repo.git.text("config", "--get", "rerere.autoupdate") == configured
    assert second.view["rerere_autoupdate"] is autoupdate
    assert second.view["rerere_autoupdate_effective"] is autoupdate
    assert len(repo.resolver.calls) == (0 if autoupdate else 1)
    assert repo.file.read_text() == "combined\n"
    if not autoupdate:
        assert repo.resolver.calls[0].marked == "combined\n"


def test_explicit_rerere_policy_does_not_retry_a_rejected_hook(repo):
    repo.git.run("config", "rerere.enabled", "true")
    repo.git.run("config", "rerere.autoupdate", "false")
    assert integrate(repo, "merge", rerere_autoupdate=True).view["status"] == "succeeded"
    repo.git.run("reset", "--hard", repo.source_oid)
    repo.resolver.calls.clear()
    hook = repo.root / ".git/hooks/pre-commit"
    count = repo.root / ".git/hook-count"
    hook.write_text("#!/bin/sh\necho rejected >> .git/hook-count\nexit 1\n")
    hook.chmod(0o755)
    audit = integrate(
        repo,
        "merge",
        rerere_autoupdate=True,
        commit_options=CommitOptions(signoff=True),
    )
    assert audit.view["status"] == "failed"
    assert count.read_text().splitlines() == ["rejected"]
    assert repo.resolver.calls == []
    assert repo.git.operation_active()


def test_autoupdate_flag_does_not_enable_disabled_rerere(repo):
    repo.git.run("config", "rerere.enabled", "false")
    audit = integrate(repo, "merge", rerere_autoupdate=True)
    assert audit.view["status"] == "succeeded", audit.view
    assert len(repo.resolver.calls) == 1
    assert repo.git.text("config", "--get", "rerere.enabled") == "false"


def test_rerere_override_is_kept_across_multiple_rebase_steps(repo, mocker):
    repo.git.run("config", "rerere.enabled", "true")
    repo.git.run("config", "rerere.autoupdate", "false")
    repo.file.write_text("second source\n")
    repo.git.run("commit", "-am", "second source change")
    original = repo.git.text("rev-parse", "HEAD")
    resolve = repo.resolver.run

    def distinct_resolutions(git, audit, round_number, paths, check):
        repo.resolver.content = f"combined {round_number}\n"
        return resolve(git, audit, round_number, paths, check)

    mocker.patch.object(repo.resolver, "run", side_effect=distinct_resolutions)
    first = integrate(repo, rerere_autoupdate=True)
    assert first.view["status"] == "succeeded", first.view
    assert len(repo.resolver.calls) == 2
    repo.git.run("reset", "--hard", original)
    repo.resolver.calls.clear()
    second = integrate(repo, rerere_autoupdate=True)
    assert second.view["status"] == "succeeded", second.view
    assert repo.resolver.calls == []
    assert repo.git.text("config", "--get", "rerere.autoupdate") == "false"
    assert repo.file.read_text() == "combined 2\n"
