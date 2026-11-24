# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Native rebase selection, replay, and empty-commit policies."""

from types import SimpleNamespace

import pytest

from git_ez.engine import Engine
from git_ez.errors import EzError
from git_ez.options import CommitOptions, RebaseOptions
from tests.fakes import FakeResolver
from tests.helpers import integrate

pytestmark = pytest.mark.integration


def test_rebasing_merge_commits_is_refused_before_creating_an_audit(repo):
    repo.git.run("checkout", "-b", "side")
    (repo.root / "side.txt").write_text("side\n")
    repo.git.run("add", ".")
    repo.git.run("commit", "-m", "Side change")
    repo.git.run("checkout", "feature")
    repo.git.run("merge", "--no-ff", "--no-edit", "side")
    before = repo.git.text("rev-parse", "HEAD")
    with pytest.raises(EzError, match="Rebasing merge commits"):
        integrate(repo)
    assert repo.git.text("rev-parse", "HEAD") == before
    assert repo.resolver.calls == []
    assert not (repo.root / ".git/ez").exists()


def test_rebase_onto_replays_only_selected_series(repo):
    repo.git.run("checkout", "-b", "old-base", repo.base_oid)
    (repo.root / "old.txt").write_text("old base only\n")
    repo.git.run("add", "old.txt")
    repo.git.run("commit", "-m", "old base")
    upstream = repo.git.text("rev-parse", "HEAD")
    repo.git.run("checkout", "-b", "topic")
    (repo.root / "topic.txt").write_text("topic\n")
    repo.git.run("add", "topic.txt")
    repo.git.run("commit", "-m", "topic change")
    audit = integrate(repo, target="old-base", onto="main")
    assert audit.view["status"] == "succeeded", audit.view
    assert repo.git.text("rev-parse", "HEAD^") == repo.parent_oid
    assert not (repo.root / "old.txt").exists()
    assert (repo.root / "topic.txt").read_text() == "topic\n"
    assert repo.git.branch_oid("old-base") == upstream
    assert repo.git.branch_oid("main") == repo.parent_oid
    assert repo.resolver.calls == []


@pytest.mark.parametrize("target", ["HEAD", "HEAD~1"])
def test_rebase_onto_pins_head_relative_inputs_and_handles_empty_series(repo, target):
    audit = integrate(repo, target=target, onto="main")
    assert audit.view["status"] == "succeeded", audit.view
    assert audit.view["target_ref"] is None
    assert audit.view["onto_oid"] == repo.parent_oid
    if target == "HEAD":
        assert repo.git.branch_oid("feature") == repo.parent_oid
        assert repo.resolver.calls == []
    else:
        assert repo.file.read_text() == "combined\n"
        assert len(repo.resolver.calls) == 1


@pytest.mark.parametrize("phase", ["preflight", "resolver", "validation"])
@pytest.mark.parametrize("changed_ref", ["main", "upstream"])
def test_rebase_onto_detects_changed_input_refs(repo, mocker, phase, changed_ref):
    repo.git.run("branch", "upstream", repo.base_oid)
    engine = Engine(repo.root, repo.config, repo.resolver)
    moved_oid = repo.source_oid
    if phase == "preflight":
        prepare = engine._prepare_run

        def prepare_and_move(*args):
            audit = prepare(*args)
            repo.git.run("update-ref", f"refs/heads/{changed_ref}", moved_oid)
            return audit

        mocker.patch.object(engine, "_prepare_run", side_effect=prepare_and_move)
    elif phase == "resolver":
        resolve = repo.resolver.run

        def resolve_and_move(*args):
            result = resolve(*args)
            repo.git.run("update-ref", f"refs/heads/{changed_ref}", moved_oid)
            return result

        mocker.patch.object(repo.resolver, "run", side_effect=resolve_and_move)
    command = (
        ("git", "update-ref", f"refs/heads/{changed_ref}", moved_oid)
        if phase == "validation"
        else ()
    )
    audit = engine.run("upstream", onto="main", validation=command)
    assert audit.view["status"] == "failed", audit.view
    assert "reference changed" in audit.view["error"] or "input refs" in audit.view["error"]
    assert audit.view["onto_oid"] == repo.parent_oid
    assert repo.git.branch_oid(changed_ref) == moved_oid
    if phase == "preflight":
        assert repo.resolver.calls == []
        assert repo.git.branch_oid("feature") == repo.source_oid
    elif phase == "resolver":
        assert repo.git.rebase_active()
    else:
        assert audit.view["integrated"]


@pytest.mark.parametrize("onto", ["missing", "--help", "main...feature"])
def test_invalid_onto_is_rejected_before_creating_audit(repo, onto):
    with pytest.raises(EzError):
        integrate(repo, onto=onto)
    assert not (repo.root / ".git/ez").exists()
    assert repo.git.branch_oid("feature") == repo.source_oid


def test_onto_is_rejected_for_merge_by_engine(repo):
    with pytest.raises(EzError, match="only supported for rebase"):
        integrate(repo, "merge", onto="main")
    assert not (repo.root / ".git/ez").exists()


def test_rebase_onto_records_a_fully_dropped_series(repo):
    repo.git.run("checkout", "main")
    repo.file.write_text("source\n")
    repo.git.run("commit", "-am", "equivalent feature change")
    new_base = repo.git.text("rev-parse", "HEAD")
    repo.git.run("checkout", "feature")
    audit = integrate(repo, target=repo.base_oid, onto="main")
    assert audit.view["status"] == "succeeded", audit.view
    assert audit.view["result_oid"] == new_base
    assert repo.resolver.calls == []


def test_rebase_onto_rejects_a_result_missing_the_new_base(repo, mocker):
    engine = Engine(repo.root, repo.config, repo.resolver)
    run = engine.git.run

    def replace_result(*args, **kwargs):
        result = run(*args, **kwargs)
        if args[0] == "rebase" and result.returncode == 0:
            repo.git.run("reset", "--hard", repo.source_oid)
        return result

    mocker.patch.object(engine.git, "run", side_effect=replace_result)
    audit = engine.run(repo.base_oid, onto="main")
    assert audit.view["status"] == "failed"
    assert "does not contain" in audit.view["error"]
    assert repo.git.branch_oid("feature") == repo.source_oid


@pytest.mark.parametrize("policy", ["drop", "keep", "stop"])
@pytest.mark.parametrize("empty_message", [False, True])
def test_empty_policy_after_conflict_resolution(repo, policy, empty_message):
    if empty_message:
        repo.git.run("commit", "--amend", "--allow-empty-message", "-m", "")
    original_author = repo.git.text("show", "-s", "--format=%an <%ae>%n%aI", "HEAD")
    repo.resolver = FakeResolver(content="parent\n")
    audit = integrate(repo, rebase_options=RebaseOptions(empty=policy))
    assert len(repo.resolver.calls) == 1
    assert audit.view["rebase_options"]["empty"] == policy
    assert audit.view["empty_commits"][0]["policy"] == policy
    if policy == "stop":
        assert audit.view["status"] == "failed"
        assert not audit.view["integrated"]
        assert repo.git.rebase_active()
        assert "resolution is empty" in audit.view["error"]
    else:
        assert audit.view["status"] == "succeeded", audit.view
        assert not repo.git.operation_active()
        if policy == "keep":
            assert repo.git.text("rev-parse", "HEAD^") == repo.parent_oid
            assert repo.git.text("show", "-s", "--format=%B", "HEAD") == (
                "" if empty_message else "source change"
            )
            assert repo.git.text("show", "-s", "--format=%an <%ae>%n%aI", "HEAD") == original_author
            assert repo.git.run("diff", "HEAD^", "HEAD").stdout == b""
        else:
            assert repo.git.branch_oid("feature") == repo.parent_oid


@pytest.mark.parametrize("policy", ["drop", "keep", "stop"])
def test_reapply_cherry_picks_obeys_empty_policy_without_model(repo, policy):
    repo.git.run("checkout", "main")
    repo.file.write_text("source\n")
    repo.git.run("commit", "-am", "equivalent change")
    target = repo.git.text("rev-parse", "HEAD")
    repo.git.run("checkout", "feature")
    audit = integrate(repo, rebase_options=RebaseOptions(empty=policy, reapply_cherry_picks=True))
    assert repo.resolver.calls == []
    if policy == "stop":
        assert audit.view["status"] == "failed"
        assert repo.git.rebase_active()
        assert not audit.view["integrated"]
    else:
        assert audit.view["status"] == "succeeded", audit.view
        if policy == "keep":
            assert repo.git.text("rev-parse", "HEAD^") == target
            assert repo.git.run("diff", "HEAD^", "HEAD").stdout == b""
        else:
            assert repo.git.branch_oid("feature") == target


@pytest.mark.parametrize("keep", [None, True, False])
def test_rebase_controls_initially_empty_commits(repo, keep):
    repo.git.run("checkout", "-b", "empty", repo.base_oid)
    repo.git.run("commit", "--allow-empty", "-m", "intentional empty commit")
    audit = integrate(repo, rebase_options=RebaseOptions(keep_empty=keep))
    assert audit.view["status"] == "succeeded", audit.view
    assert repo.resolver.calls == []
    if keep is False:
        assert repo.git.branch_oid("empty") == repo.parent_oid
    else:
        assert repo.git.text("rev-parse", "HEAD^") == repo.parent_oid
        assert repo.git.text("show", "-s", "--format=%s", "HEAD") == "intentional empty commit"


@pytest.mark.parametrize("force", [True, False])
def test_force_rebase_controls_replay_of_unchanged_series(repo, monkeypatch, force):
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2001-01-01T00:00:00+00:00")
    audit = integrate(repo, target=repo.base_oid, rebase_options=RebaseOptions(force_rebase=force))
    assert audit.view["status"] == "succeeded", audit.view
    assert (repo.git.branch_oid("feature") != repo.source_oid) is force
    assert repo.resolver.calls == []


def test_empty_keep_honors_commit_hook_failure(repo):
    repo.resolver = FakeResolver(content="parent\n")
    hook = repo.root / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\necho empty-commit-rejected >&2\nexit 1\n")
    hook.chmod(0o755)
    audit = integrate(repo, rebase_options=RebaseOptions(empty="keep"))
    assert audit.view["status"] == "failed"
    assert "empty-commit-rejected" in audit.view["error"]
    assert repo.git.rebase_active()


def test_merge_rejects_rebase_options(repo):
    with pytest.raises(EzError, match="only supported for rebase"):
        integrate(repo, "merge", rebase_options=RebaseOptions(empty="keep"))
    assert not (repo.root / ".git/ez").exists()


@pytest.mark.parametrize("reapply", [True, False])
def test_reapply_cherry_picks_controls_equivalent_commit_replay(repo, reapply):
    repo.git.run("checkout", "-b", "equivalent", repo.base_oid)
    repo.file.write_text("source\n")
    repo.git.run("commit", "-am", "upstream equivalent")
    target = repo.git.text("rev-parse", "HEAD")
    repo.git.run("checkout", "feature")
    audit = integrate(
        repo,
        target="equivalent",
        rebase_options=RebaseOptions(empty="keep", reapply_cherry_picks=reapply),
    )
    assert audit.view["status"] == "succeeded", audit.view
    assert repo.resolver.calls == []
    if reapply:
        assert repo.git.text("rev-parse", "HEAD^") == target
        assert repo.git.text("show", "-s", "--format=%s", "HEAD") == "source change"
    else:
        assert repo.git.branch_oid("feature") == target


def selection_history(repo, *, diverged=True, equivalent=True, fixup=False):
    repo.git.run("checkout", "-b", "upstream", repo.base_oid)
    (repo.root / "old.txt").write_text("old upstream\n")
    repo.git.run("add", ".")
    repo.git.run("commit", "-m", "old upstream")
    point = repo.git.text("rev-parse", "HEAD")
    repo.git.run("checkout", "-b", "selection-topic")
    (repo.root / "a.txt").write_text("feature A\n")
    repo.git.run("add", ".")
    repo.git.run("commit", "-m", "feature A")
    feature = repo.git.text("rev-parse", "HEAD")
    (repo.root / "b.txt").write_text("feature B\n")
    repo.git.run("add", ".")
    repo.git.run("commit", "-m", "feature B")
    if fixup:
        (repo.root / "a.txt").write_text("fixed A\n")
        repo.git.run("commit", "-am", "fixup! feature A")
    before = repo.git.text("rev-parse", "HEAD")
    repo.git.run("checkout", "upstream")
    repo.git.run("reset", "--hard", repo.base_oid)
    if diverged:
        (repo.root / "new.txt").write_text("new upstream\n")
        repo.git.run("add", ".")
        repo.git.run("commit", "-m", "new upstream")
    if equivalent:
        repo.git.run("cherry-pick", feature)
    target = repo.git.text("rev-parse", "HEAD")
    repo.git.run("checkout", "selection-topic")
    return SimpleNamespace(point=point, before=before, target=target)


def test_keep_base_preserves_the_base_without_claiming_upstream_ancestry(repo):
    audit = integrate(repo, rebase_options=RebaseOptions(keep_base=True))
    assert audit.view["status"] == "succeeded", audit.view
    assert repo.git.branch_oid("feature") == repo.source_oid
    assert (
        repo.git.run("merge-base", "--is-ancestor", repo.parent_oid, "HEAD", check=False).returncode
        == 1
    )
    plan = audit.view["rebase_plan"]
    assert plan["base_oid"] == repo.base_oid
    assert plan["selection_oid"] == repo.base_oid
    assert plan["reapply_cherry_picks"] is True
    assert plan["fork_point"]["enabled"] is False
    assert repo.resolver.calls == []


@pytest.mark.parametrize(
    "keep,fork,reapply,diverged,equivalent,autosquash",
    [
        (False, True, None, False, False, False),
        (False, True, None, True, True, False),
        (False, True, True, True, True, False),
        (True, False, None, True, True, False),
        (True, True, None, True, True, False),
        (True, True, False, True, True, False),
        (True, False, False, True, True, False),
        (False, True, None, True, True, True),
        (True, True, None, True, False, True),
    ],
)
def test_pinned_selection_matches_native_rebase(
    repo, keep, fork, reapply, diverged, equivalent, autosquash
):
    history = selection_history(repo, diverged=diverged, equivalent=equivalent, fixup=autosquash)
    base = repo.git.text("merge-base", history.target, history.before) if keep else history.target
    repo.git.run("checkout", "-b", "native-selection", history.before)
    args = ["--merge", "--no-autostash", "--no-update-refs", "--no-rebase-merges", "--empty=keep"]
    if keep:
        args.append("--keep-base")
    args.append("--fork-point" if fork else "--no-fork-point")
    if reapply is not None:
        args.append("--reapply-cherry-picks" if reapply else "--no-reapply-cherry-picks")
    if autosquash:
        args += ["--interactive", "--autosquash"]
    repo.git.run("rebase", *args, "upstream")
    expected_tree = repo.git.text("rev-parse", "HEAD^{tree}")
    expected_messages = repo.git.text("log", "--reverse", "--format=%B%x00", f"{base}..HEAD")
    repo.git.run("checkout", "selection-topic")
    refs = repo.git.text("for-each-ref", "--format=%(refname)")
    options = RebaseOptions(
        keep_base=keep,
        fork_point=fork,
        reapply_cherry_picks=reapply,
        autosquash=autosquash,
        empty="keep",
    )
    audit = integrate(repo, target="upstream", rebase_options=options)
    assert audit.view["status"] == "succeeded", audit.view
    assert repo.git.text("rev-parse", "HEAD^{tree}") == expected_tree
    assert (
        repo.git.text("log", "--reverse", "--format=%B%x00", f"{base}..HEAD") == expected_messages
    )
    assert repo.git.text("for-each-ref", "--format=%(refname)") == refs
    assert repo.resolver.calls == []
    plan = audit.view["rebase_plan"]
    assert plan["base_oid"] == base
    assert plan["fork_point"]["oid"] == (history.point if fork else None)


@pytest.mark.parametrize(
    "keep,configured,requested,enabled",
    [
        (False, True, None, False),
        (True, True, None, False),
        (False, False, None, False),
        (True, False, None, False),
        (False, False, True, True),
        (True, False, True, True),
        (False, True, False, False),
        (True, True, False, False),
    ],
)
def test_fork_point_defaults_and_cli_match_native_configuration(
    repo, configured, requested, enabled, keep
):
    history = selection_history(repo, diverged=False, equivalent=False)
    repo.git.run("config", "rebase.forkPoint", str(configured).lower())
    repo.git.run("checkout", "-b", "native-configured", history.before)
    flags = ("--keep-base",) if keep else ()
    if requested is not None:
        flags += ("--fork-point" if requested else "--no-fork-point",)
    repo.git.run("rebase", "--merge", "--no-autostash", "--no-update-refs", *flags, "upstream")
    expected_tree = repo.git.text("rev-parse", "HEAD^{tree}")
    expected_messages = repo.git.text("log", "--format=%B%x00", f"{history.target}..HEAD")
    repo.git.run("checkout", "selection-topic")
    audit = integrate(
        repo, target="upstream", rebase_options=RebaseOptions(fork_point=requested, keep_base=keep)
    )
    assert audit.view["status"] == "succeeded", audit.view
    assert repo.git.text("rev-parse", "HEAD^{tree}") == expected_tree
    assert repo.git.text("log", "--format=%B%x00", f"{history.target}..HEAD") == expected_messages
    assert audit.view["rebase_plan"]["fork_point"]["enabled"] is enabled
    assert repo.git.text("rev-list", "--count", f"{history.target}..HEAD") == (
        "2" if enabled else "3"
    )
    assert (repo.root / "old.txt").exists() is (not enabled)
    assert repo.git.text("config", "--get", "rebase.forkPoint") == str(configured).lower()


@pytest.mark.parametrize("keep", [False, True])
def test_fork_point_fallback_is_explicit_when_reflog_has_no_usable_entry(repo, keep):
    selection_history(repo, equivalent=False)
    repo.git.run("reflog", "expire", "--expire=all", "refs/heads/upstream")
    audit = integrate(
        repo, target="upstream", rebase_options=RebaseOptions(fork_point=True, keep_base=keep)
    )
    assert audit.view["status"] == "succeeded", audit.view
    assert audit.view["rebase_plan"]["fork_point"]["status"] == "fallback"
    assert (repo.root / "old.txt").exists()


def test_computed_fork_point_survives_reflog_expiry_before_execution(repo, mocker):
    history = selection_history(repo, diverged=False, equivalent=False)
    engine = Engine(repo.root, repo.config, repo.resolver)
    prepare = engine._prepare_run

    def expire(*args):
        audit = prepare(*args)
        repo.git.run("reflog", "expire", "--expire=all", "refs/heads/upstream")
        return audit

    mocker.patch.object(engine, "_prepare_run", side_effect=expire)
    audit = engine.run("upstream", rebase_options=RebaseOptions(fork_point=True))
    assert audit.view["status"] == "succeeded", audit.view
    assert audit.view["rebase_plan"]["fork_point"]["oid"] == history.point
    assert repo.git.text("rev-list", "--count", f"{history.target}..HEAD") == "2"
    assert not (repo.root / "old.txt").exists()


def test_keep_base_rejects_onto_before_creating_a_run(repo):
    with pytest.raises(EzError, match="cannot be used together"):
        integrate(repo, onto="main", rebase_options=RebaseOptions(keep_base=True))
    assert not (repo.root / ".git/ez").exists()
    assert repo.git.branch_oid("feature") == repo.source_oid


def test_keep_base_requires_a_unique_merge_base(repo):
    left_tree = repo.git.text("rev-parse", f"{repo.source_oid}^{{tree}}")
    right_tree = repo.git.text("rev-parse", f"{repo.parent_oid}^{{tree}}")
    left = repo.git.text(
        "commit-tree", left_tree, "-p", repo.source_oid, "-p", repo.parent_oid, "-m", "left merge"
    )
    right = repo.git.text(
        "commit-tree", right_tree, "-p", repo.parent_oid, "-p", repo.source_oid, "-m", "right merge"
    )
    repo.git.run("branch", "ambiguous", right)
    repo.git.run("reset", "--hard", left)
    with pytest.raises(EzError, match="exactly one merge base"):
        integrate(repo, target="ambiguous", rebase_options=RebaseOptions(keep_base=True))
    assert not (repo.root / ".git/ez").exists()
    assert repo.git.branch_oid("feature") == left


@pytest.mark.parametrize("target", ["literal", "expression"])
def test_fork_point_requires_a_named_reference(repo, target):
    revision = repo.parent_oid if target == "literal" else "main^0"
    with pytest.raises(EzError, match="requires a named upstream reference"):
        integrate(repo, target=revision, rebase_options=RebaseOptions(fork_point=True))
    assert not (repo.root / ".git/ez").exists()


def test_fork_point_conflict_guards_the_original_upstream(repo, mocker):
    history = selection_history(repo)
    repo.git.run("checkout", "upstream")
    (repo.root / "b.txt").write_text("upstream B\n")
    repo.git.run("add", ".")
    repo.git.run("commit", "-m", "upstream B")
    repo.git.run("checkout", "selection-topic")
    resolve = repo.resolver.run

    def move_upstream(*args):
        decision = resolve(*args)
        repo.git.run("update-ref", "refs/heads/upstream", repo.base_oid)
        return decision

    mocker.patch.object(repo.resolver, "run", side_effect=move_upstream)
    audit = integrate(repo, target="upstream", rebase_options=RebaseOptions(fork_point=True))
    assert audit.view["status"] == "failed"
    assert "input refs" in audit.view["error"]
    assert repo.git.rebase_active()
    assert repo.git.branch_oid("selection-topic") == history.before


def test_fork_point_combines_with_a_separate_onto_base_like_native_git(repo):
    history = selection_history(repo)
    repo.git.run("checkout", "-b", "native-onto", history.before)
    repo.git.run("rebase", "--merge", "--fork-point", "--empty=keep", "--onto", "main", "upstream")
    expected = repo.git.text("rev-parse", "HEAD^{tree}")
    messages = repo.git.text("log", "--format=%B%x00", f"{repo.parent_oid}..HEAD")
    repo.git.run("checkout", "selection-topic")
    audit = integrate(
        repo,
        target="upstream",
        onto="main",
        rebase_options=RebaseOptions(fork_point=True, empty="keep"),
    )
    assert audit.view["status"] == "succeeded", audit.view
    assert repo.git.text("rev-parse", "HEAD^{tree}") == expected
    assert repo.git.text("log", "--format=%B%x00", f"{repo.parent_oid}..HEAD") == messages
    assert audit.view["rebase_plan"]["base_oid"] == repo.parent_oid


@pytest.mark.parametrize("phase", ["preflight", "validation"])
def test_keep_base_still_guards_the_upstream_reference(repo, mocker, phase):
    engine = Engine(repo.root, repo.config, repo.resolver)
    prepare = engine._prepare_run

    def move_ref(*args):
        audit = prepare(*args)
        repo.git.run("update-ref", "refs/heads/main", repo.source_oid)
        return audit

    if phase == "preflight":
        mocker.patch.object(engine, "_prepare_run", side_effect=move_ref)
    validation = (
        ("git", "update-ref", "refs/heads/main", repo.source_oid) if phase == "validation" else ()
    )
    audit = engine.run("main", validation=validation, rebase_options=RebaseOptions(keep_base=True))
    assert audit.view["status"] == "failed"
    assert "Target reference changed" in audit.view["error"]
    assert audit.view["integrated"] is (phase == "validation")
    assert repo.git.branch_oid("feature") == repo.source_oid
    assert repo.git.branch_oid("main") == repo.source_oid
    assert repo.resolver.calls == []


def test_keep_base_rejects_histories_without_a_common_base(repo):
    repo.git.run("checkout", "--orphan", "unrelated")
    repo.git.run("rm", "-rf", ".")
    (repo.root / "orphan.txt").write_text("orphan\n")
    repo.git.run("add", ".")
    repo.git.run("commit", "-m", "orphan")
    repo.git.run("checkout", "feature")
    with pytest.raises(EzError, match="exactly one merge base"):
        integrate(repo, target="unrelated", rebase_options=RebaseOptions(keep_base=True))
    assert not (repo.root / ".git/ez").exists()


def test_no_fork_point_allows_literal_targets_with_configured_fork_point(repo):
    repo.git.run("config", "rebase.forkPoint", "true")
    audit = integrate(repo, target=repo.parent_oid, rebase_options=RebaseOptions(fork_point=False))
    assert audit.view["status"] == "succeeded", audit.view
    assert audit.view["rebase_plan"]["fork_point"]["status"] == "disabled"


def test_internal_selection_object_does_not_change_the_signing_policy(repo):
    selection_history(repo)
    repo.git.run("config", "commit.gpgSign", "true")
    repo.git.run("config", "gpg.program", "/nonexistent/selection-test-signing-program")
    audit = integrate(
        repo,
        target="upstream",
        rebase_options=RebaseOptions(fork_point=True),
        commit_options=CommitOptions(gpg_sign=False),
    )
    assert audit.view["status"] == "succeeded", audit.view
    assert repo.git.text("config", "--get", "commit.gpgSign") == "true"
    assert "gpgsig " not in repo.git.text("cat-file", "commit", "HEAD")


@pytest.mark.parametrize("annotated", [False, True])
def test_named_tag_fork_point_preserves_native_fallback_diagnostics(repo, annotated):
    args = ("-a", "-m", "upstream tag") if annotated else ()
    repo.git.run("tag", *args, "upstream-tag", repo.base_oid)
    native = repo.git.run(
        "merge-base", "--fork-point", "refs/tags/upstream-tag", repo.source_oid, check=False
    )
    audit = integrate(repo, target="upstream-tag", rebase_options=RebaseOptions(fork_point=True))
    assert audit.view["status"] == "succeeded", audit.view
    fork = audit.view["rebase_plan"]["fork_point"]
    assert fork["status"] == ("found" if native.returncode == 0 else "fallback")
    assert fork.get("diagnostic", "") == native.stderr.decode(errors="replace").strip()
    assert repo.git.branch_oid("feature") == repo.source_oid
    assert repo.resolver.calls == []


def test_fork_point_selection_preserves_explicit_result_signing(repo, ssh_signing):
    selection_history(repo)
    audit = integrate(
        repo,
        target="upstream",
        rebase_options=RebaseOptions(fork_point=True),
        commit_options=CommitOptions(gpg_sign=str(ssh_signing)),
    )
    assert audit.view["status"] == "succeeded", audit.view
    repo.git.run("verify-commit", "HEAD")


@pytest.mark.parametrize("equivalent,fixup", [(True, False), (False, False), (False, True)])
def test_fork_point_onto_itself_matches_native_selection(repo, equivalent, fixup):
    history = selection_history(repo, equivalent=equivalent, fixup=fixup)
    repo.git.run("config", "rebase.autoSquash", "true")
    repo.git.run("checkout", "-b", "native-point", history.before)
    repo.git.run("rebase", "--merge", "--fork-point", "--onto", history.point, "upstream")
    expected_tree = repo.git.text("rev-parse", "HEAD^{tree}")
    messages = repo.git.text("log", "--format=%B%x00", f"{history.point}..HEAD")
    native_head = repo.git.text("rev-parse", "HEAD")
    repo.git.run("checkout", "selection-topic")
    audit = integrate(
        repo,
        target="upstream",
        onto=history.point,
        rebase_options=RebaseOptions(fork_point=True),
    )
    assert audit.view["status"] == "succeeded", audit.view
    assert repo.git.text("rev-parse", "HEAD^{tree}") == expected_tree
    assert repo.git.text("log", "--format=%B%x00", f"{history.point}..HEAD") == messages
    assert audit.view["rebase_plan"]["requires_sequence"]
    assert audit.view["rebase_sequence"]["status"] == "captured"
    if native_head == history.before:
        assert repo.git.text("rev-parse", "HEAD") == history.before
