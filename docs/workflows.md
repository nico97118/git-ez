# Git workflows and execution policies

[README](../README.md) · [Command manual](man/git-ez.1) · [Audit](audit.md)

## Commands

```bash
# On the branch to rebase:
git ez rebase main

# On the branch that should receive feature:
git ez merge feature

# Explicit repository and final validation:
git ez rebase origin/main --repo /path/to/project --check python -B -m pytest

# Probe model compatibility without exposing the repository or shell tools:
git ez probe
# Save the JSON report to a file instead:
git ez probe --output /tmp/ai-compatibility.json

# Read an existing report:
git ez show /path/to/project/.git/ez/runs/RUN_ID

git ez --version
```

The target can be a branch, an already-fetched reference, a tag, a SHA, or an expression
such as `HEAD~2`. It is resolved and pinned at startup. The current repository is used by
default, including when invoked from a subdirectory. A checked-out branch and a clean
checkout are required.

### Checkout locking

Only one `git ez rebase` or `git ez merge` can run per checkout at a time. A second
invocation fails immediately, before repository checks, audit creation, or model calls.
The lock covers the entire run, including interactive review, validation, and final audit
capture. Linked worktrees have separate locks.

The operating system releases the lock when the process exits, including abrupt
termination. The empty `ez-execution.lock` file remains in the checkout's Git directory;
its presence does not mean a run is active, and it must not be deleted while a run is
active. Existing Git operations and local changes remain available for inspection and
native Git recovery. This advisory lock coordinates git-ez invocations only; native Git,
editors, and other tools can still change the repository, so git-ez's state checks remain
necessary.

Merge lets Git fast-forward or create a commit according to its configuration.
Rebase replays the current branch onto the target. This interface supports simple
integration, without the full set of Git options (interactive rebase, squash, octopus
merge, etc.). Git aliases are optional: `git config --global alias.ai-rebase 'ez rebase'`.

### Validation, diagnostics, and exit codes

`--check` must be the last option. All following arguments form a command executed
without a shell at the target repository root. Its environment must provide the project's
dependencies. The agent can also run it while making corrections; it is run again after
integration. A nonzero exit code, timeout, or unexpected checkout change invalidates the
run. Test artifacts must be ignored or cleaned up by the target project.

The probe checks a text response, a native call to the harmless `compatibility_echo`
tool, and subsequent use of its result, through the same connectors used for resolution.
It does not modify a repository or execute model commands. A `compatible` result is not
a measure of conflict-resolution quality. By default, the JSON report is written to stdout;
use `--output <file>` to save it to a file instead. Diagnostics go to stderr in both modes.

Exit codes: `0` = successful local integration, displayed report, or compatible probe;
`1` = failed run/probe; `2` = configuration, CLI, or precondition error.

### Interactive proposal review

Use `git ez merge <target> --interactive` or `git ez rebase <target> --interactive`
to review each harness proposal before the engine stages it. This reviews conflict
resolutions; it does not open Git's interactive rebase sequence editor.

The terminal shows the diff, captured file contents (including additions), capture
limits, and the agent's rationale with reported checks and uncertainties. Final
validation runs later. Choose **accept**, **revise** (then enter feedback), or
**stop**. Revisions start from the current proposal and require another explicit
approval. Empty or invalid input never accepts; EOF and interruption stop the run.
Engine confidence, file, reference and operation checks still apply after approval.
Changes during review invalidate approval. Each attempt, feedback and decision is
retained in the audit; acceptance and actual application are recorded separately.

Stopping preserves proposed files, the unresolved index and the active Git operation.
Inspect the report and use native `git merge` / `git rebase` continuation or abort
commands as appropriate. No automatic rollback occurs. Without conflicts there is
no prompt or model call. Resolutions already staged by native rerere autoupdate
keep their existing path and do not receive harness proposal review.

## Supported Git workflow

Use git-ez for a simple integration into the currently checked-out branch. It exposes
its own small command-line interface; it does not forward arbitrary options to
`git rebase` or `git merge`.

| Contract | Behavior |
| --- | --- |
| Target | One explicit revision is required, even when the branch has an upstream. It must already be available locally. |
| Repository | A clean checkout on a local branch, with no active Git operation. Detached HEAD and bare repositories are unsupported. |
| Rebase | Replay the current branch using Git's merge backend. Use `--onto` for a separate new base or `--keep-base` to retain the unique common ancestor. Autostash, update-refs, and rebasing merge commits are disabled. |
| Merge | Integrate one pinned target with `--no-edit`; Git's fast-forward policy still applies. |
| Interaction | Git runs noninteractively with `diff3` conflict markers. Hooks and signing failures are not bypassed. |
| Model | Valid `ez.*` model configuration is required before integration, even if no conflicts occur. No model call is made when Git resolves the operation without assistance. |
| Recovery | Failed runs preserve local state. Inspect the audit and use native Git to continue or abort an active operation. |

Interactive editing of a rebase, `--rebase-merges`, merge `--squash`,
`--no-commit`, octopus merges, and arbitrary strategy options are outside this interface.
For these workflows, use Git directly. git-ez cannot attach to an operation that was
already started with Git.

### Rebase base and replay selection

Rebase accepts `--onto <newbase> <target>`. Here, target is the upstream boundary
used to select commits, and newbase is where Git replays them. Both revisions are
pinned and their named references are checked throughout the run. The audit and
range-diff distinguish the original series from the series on the new base.
Only the current branch is rebased; a second positional branch argument and
Git's `A...B` shorthand for the new base are not supported.

```sh
git ez rebase --onto main old-base
```

Rebase accepts `--keep-base` / `--no-keep-base` and `--fork-point` /
`--no-fork-point`. `--keep-base` retains the unique merge base of the current
branch and upstream as the result's base. It implies cherry-pick reapplication
unless `--no-reapply-cherry-picks` is explicit. It cannot be combined with
`--onto`; absent or ambiguous merge bases are rejected before integration.
In this mode, `Integrated: True` means the requested rebase finished; the
upstream tip need not be an ancestor of the result.

`--fork-point` refines replay selection from the upstream's local reflog.
The last explicit CLI value wins. Like native Git with an explicit upstream,
fork-point defaults to disabled, including with `--keep-base` or
`rebase.forkPoint=true`. That configuration only disables an implicit default
when false; it does not enable fork-point for an explicit upstream. Use
`--fork-point` to opt in. Fork-point mode requires a named reference (a local or
remote-tracking branch, tag, or HEAD); use `--no-fork-point` for a literal SHA or
revision expression. A missing usable reflog point falls back to the otherwise
chosen replay boundary, with the reason recorded in the report.

git-ez pins the base and computed fork point before running Git, rather than
letting Git recompute them during replay. It passes explicit `--onto` and
`--no-fork-point` arguments with those pinned selections. When both exclusion
histories are needed and neither contains the other, an internal merge object represents their
combined exclusions so native cherry-pick equivalence remains intact. This
creates no branch or additional reference, and the object is not part of the
result history. This object is unsigned metadata; signing options still apply to
the resulting commits. The audit records its command, parents, tree and bounded raw
capture, including any truncation. Git's pre-rebase hook receives the effective
pinned replay boundary, which may be this internal object, rather than the
original upstream name. Hooks are still executed and their failures remain terminal.
Hooks that require the original upstream argument should use native Git directly.

When `--onto` equals the fork point and combining exclusions changes the replay
boundary, git-ez uses the native interactive sequencer to avoid an incorrect
up-to-date shortcut. Its capture-only editor records the generated instructions
without changing them. Autosquash stays disabled unless explicitly requested,
the default empty-commit policy remains `drop`, and per-commit reuse is preserved.
The instruction-format override and bounded sequence capture described below also
apply to this case.

The original upstream and any explicit new-base reference remain monitored.
Final ancestry is checked against the pinned result base; range-diff compares
the selected candidate series with its replay on that base. These options combine
with autosquash and signing without changing their scope.

```sh
git ez rebase main --keep-base --autosquash
git ez rebase origin/main --fork-point
git ez rebase old-base --onto main --fork-point
```

### Empty commits and forced replay

Rebase also accepts `--empty=drop|keep|stop`, `--keep-empty` /
`--no-keep-empty`, `--reapply-cherry-picks` / `--no-reapply-cherry-picks`, and
`--force-rebase` (`-f`, `--no-ff`; reset with `--ff`). `--keep-empty` controls
commits that were empty originally; `--empty` controls commits that become empty.
Reapplying cherry-picks lets the empty policy handle changes already present
upstream. Without explicit options, native Git defaults apply.

An explicit `--empty` policy also applies after an assisted conflict resolution:
git-ez retains an empty commit for `keep`, allows Git to drop it for `drop`, and
stops with a nonzero exit code and the active rebase preserved for `stop`.
This extends native continuation behavior, which can drop empty resolutions even
with `--empty=keep`. Retained commits preserve the original author and message;
hooks and signing remain active. The audit records the policy and empty resolution.

```sh
git ez rebase main --reapply-cherry-picks --empty=keep
git ez rebase main --no-keep-empty --force-rebase
```

### Autosquash and sequence capture

Rebase accepts `--autosquash` / `--no-autosquash` (last explicit value wins).
With `--autosquash`, native Git matches and groups `fixup!`, `squash!`, and
`amend!` commits. Unmatched markers remain ordinary commits. The model only
resolves conflicts; it does not choose the order or write the combined messages.
Git determines the resulting messages without opening an editor.

Autosquash is opt-in: `rebase.autoSquash` alone does not activate it. git-ez uses
Git's interactive machinery internally with a capture-only sequence editor,
forces `rebase.instructionFormat=%s` for that invocation, and keeps `--empty=drop`
as the default rather than adopting interactive rebase's stop behavior. Explicit
`--empty` policies still apply to replayed commits; empty fixup/squash resolutions
are handled by Git as part of their existing group. Hooks and signing are retained.
Interactive editing and arbitrary todo instructions remain unsupported.

The audit retains `rebase-todo.txt` before any replay. If the complete sequence
cannot be captured within `ez.maxFileBytes`, Git stops before replay. Missing
sequence evidence prevents overall success, even if integration has completed.
The capture editor also writes `rebase-todo.txt.sha256` before replay; changes to
the sequence or its fingerprint, including from Git hooks, make the run fail.
`rebase-commits.log` records the resulting commits and messages; any truncation
at `ez.maxFileBytes` is explicit in the manifest and report. Failures preserve the
actual local state and available evidence for inspection and native recovery.

```sh
git ez rebase main --autosquash
git ez rebase old-base --onto main --autosquash --gpg-sign
```

### Merge fast-forward policy

Merge accepts `--ff`, `--no-ff`, and `--ff-only`. An explicit policy overrides
Git configuration; when repeated, the last policy wins. Without one, Git's
configuration and defaults apply. `--ff` allows fast-forward and otherwise creates
a merge commit. `--no-ff` creates a merge commit when integration is needed;
an already up-to-date branch stays unchanged. `--ff-only` refuses divergent
histories without calling the model or changing the checkout. `--ff-only` is
merge-only; in rebase, `--no-ff` forces replay and `--ff` resets that request.
Put these options before `--check`, which consumes all
remaining arguments. The requested policy is recorded in the audit and report.

```sh
git ez merge feature --no-ff
git ez merge --ff-only origin/main
git ez merge feature --ff --check python -B -m pytest
```

### Merge messages

For merge commits, use `-m <message>` / `--message <message>` (repeat for multiple
paragraphs), or `-F <file>` / `--file <file>`. Message files are read as UTF-8 before
integration, relative to the invocation directory even with `--repo`; standard
input (`-F -`) is unsupported. The requested message is retained through conflict
resolution and recorded in the audit. Message options do not force a merge commit
when Git can fast-forward.

Merge also accepts `--log[=<n>]` / `--no-log` and
`--cleanup=strip|whitespace|verbatim|scissors|default`. Bare `--log` includes up to
20 incoming commit subjects; attach a different nonnegative limit as `--log=5`.
`--no-log` is equivalent to `--log=0`. The last explicit log option wins; when
omitted, Git's `merge.log` configuration applies. Git generates the summary,
including when combined with `-m` or `-F`.

Cleanup uses Git's native message rules without opening an editor. The explicit
mode is applied both during the initial merge and when committing after a conflict;
Git's prepared `MERGE_MSG`, including its shortlog, is preserved for continuation.
In particular, `scissors` follows Git's noninteractive behavior rather than forcing
editor-dependent trimming. Omitted cleanup uses native defaults/configuration.
These options do not force a commit on fast-forward and are recorded in the audit.

```sh
git ez merge feature --log=5 --cleanup=strip
git ez merge feature --no-log -m "Integrate feature"
```

### Signing and signoff

Both operations accept `--signoff` / `--no-signoff` and `-S` /
`--gpg-sign[=<key>]` / `--no-gpg-sign`. Attach an explicit signing key with
`-S<key>` or `--gpg-sign=<key>`; a bare signing option uses Git's configured key.
These options apply to newly created commits, including after conflicts and for
retained empty commits. Signoff adds a contribution trailer; it is distinct from
a cryptographic signature. Signing and hook failures stop the run without retry
or fallback to an unsigned commit. The options do not add trailers or signatures
to existing commits reused by Git.

Merge additionally accepts `--verify-signatures` / `--no-verify-signatures`,
checking the incoming tip commit according to Git's trust configuration. This
does not verify every incoming commit or the signature of an annotated tag.
CLI options override the corresponding Git settings; omitted options preserve
native configuration. All these options must precede `--check`.

```sh
git ez merge feature --no-ff -m "Merge feature" --signoff
git ez rebase main --gpg-sign --signoff
git ez merge feature --verify-signatures
```

### Rerere

Both operations accept `--rerere-autoupdate` / `--no-rerere-autoupdate`, overriding
`rerere.autoupdate` for the whole run, including continuation, without changing
stored Git configuration. This does not enable `rerere.enabled`.
When enabled rerere reuses and stages a recorded resolution, git-ez records the
native resolution and continues without calling the model for that conflict.
With `--no-rerere-autoupdate`, Git may reuse file contents but leaves the index
unmerged: git-ez sends those conflicts through its normal assisted resolution and
validation before staging. The audit records requested and effective autoupdate.

```sh
git ez rebase main --rerere-autoupdate
git ez merge feature --no-rerere-autoupdate
```

### Caller configuration

Native caller configuration is retained except for the explicit execution policies
above and repository-redirection environment variables described under Limitations.
For example, `git -C /path/to/project -c ez.model=another-model ez rebase main` uses
Git's own directory and temporary-configuration options.
