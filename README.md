# git-ez

A Git extension for **local rebase and merge with AI-assisted conflict resolution**,
with an audit trail of changes, decisions, and their rationale.

```bash
git ez rebase main
git ez merge feature
```

The operation modifies the current branch. Users decide whether to use a dedicated
branch and whether to push the result afterward. git-ez does not create branches or
worktrees, fetch, or push.

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

Native caller configuration is retained except for the explicit execution policies
above and repository-redirection environment variables described under Limitations.
For example, `git -C /path/to/project -c ez.model=another-model ez rebase main` uses
Git's own directory and temporary-configuration options.

## Installation

Requires Python 3.11+, Git 2.38+, and macOS or Linux for local shell execution.
Using [uv](https://docs.astral.sh/uv/getting-started/installation/), install from the public repository:

```bash
uv tool install git+https://github.com/nico97118/git-ez
```

Or install from a local checkout:

```bash
uv tool install .
```

The Python distribution is named `git-ez-ai`; the Git extension and repository are
named `git-ez`. PyPI installation will use `uv tool install git-ez-ai` once a release
is published. Until then, use the GitHub or local installation above.

The package installs a single executable, `git-ez`. Once the directory reported by
`uv tool dir --bin` is on `PATH`, Git automatically discovers it as `git ez`.
A standard installation copies the package into an isolated environment: the source
checkout is not required at runtime. Provider connectors and the agent runtime are
included in the main dependencies.

### Command help and manual

`git ez -h` and `git ez rebase -h` display command-line usage. Git handles
`git ez --help` and `git help ez` through its manual viewer. The package includes
`git-ez.1`; for a uv tool installation, add its manual directory to your shell profile:

```bash
export MANPATH="$(uv tool dir)/git-ez-ai/share/man:${MANPATH:-}"
```

The trailing separator preserves the system manual search path. A working `man`
viewer is required (for example, `man-db` on Debian/Ubuntu). For other Python
installations, the manual is under `<environment-prefix>/share/man/man1/git-ez.1`.

## Git configuration

All settings use `git config`. For an OpenAI-compatible Chat Completions API
(vLLM, LM Studio, a hosted service, or a proxy):

```bash
git config --global ez.provider openai
git config --global ez.model exact-model-id
git config --global ez.endpoint https://ai-server.example/v1

# If the server uses an internal CA or requires a token:
git config --global ez.caBundle /absolute/path/to/internal-ca.pem
git config --global ez.apiKeyEnv INTERNAL_AI_TOKEN
```

The selected environment variable supplies the token. `ez.apiKey` also accepts a direct
token and takes precedence over that variable. Git stores it as plaintext in its
configuration file; prefer environment variables for secrets. The token is redacted
from archived configuration and removed from the agent shell environment.

Each key follows native Git precedence: **`git -c` > worktree/local > global > system**.
Git's `include` and `includeIf` mechanisms remain available. Setting names are
case-insensitive. To override a single setting:

```bash
git config --local ez.model project-model
git -c ez.model=another-model ez rebase main
```

| Key | Purpose / default |
| --- | --- |
| `ez.provider` | `openai` or `ollama`; default: `openai` |
| `ez.model` | Required model identifier |
| `ez.endpoint` | Server URL; defaults to the OpenAI API or local Ollama, depending on the provider |
| `ez.caBundle` | Optional trusted PEM CA bundle |
| `ez.apiKeyEnv` | Token environment variable; default: `OPENAI_API_KEY` or `LOCAL_MODEL_API_KEY` |
| `ez.apiKey` | Optional direct token, redacted from the audit |
| `ez.allowInsecureHttp` | Explicitly allow non-loopback HTTP; default: `false` |
| `ez.timeout` | HTTP, agent command, and validation timeout; default: 120 seconds |
| `ez.minConfidence` | Confidence threshold; default: `0.8` |
| `ez.maxFileBytes` | Maximum proposal size and per-file audit capture; default: 128 KiB |
| `ez.maxRounds` | Maximum number of conflict stops; default: 100 |
| `ez.modelContextTokens` | Server context window; default: 32768 |
| `ez.modelMaxOutputTokens` | Output token reserve; default: 8192 |
| `ez.agentMaxTurns` | Model call budget per session; default: 40 |
| `ez.agentMaxToolCalls` | Tool call budget per session; default: 100 |
| `ez.agentMaxContextBytes` | Additional context size limit; default: 1000000 bytes |
| `ez.toolOutputBytes` | Captured shell output limit; default: 32768 bytes |
| `ez.requestOptions` | JSON object containing server options |

Booleans and integers use Git formats (`yes`, `false`, `256k`, etc.). `ez.caBundle`
paths support `~`. Relative paths resolve from the repository root, or from the current
directory outside a repository; use an absolute path in global configuration.
No TOML or `.env` files are loaded.

A higher-priority scope replaces `ez.requestOptions` in full. For example, if supported
by the server and model:

```bash
git config --global ez.requestOptions '{"chat_template_kwargs":{"enable_thinking":false}}'
```

See [examples/ez.gitconfig](examples/ez.gitconfig) for the standard Git configuration format.
Model names, thinking options, and tool parsers must match the actual server configuration.

### Connectors

The `openai` provider explicitly uses Chat Completions and native tool calls, without
Responses or automatic fallback to another server. The endpoint accepts either a base
URL (`…/v1`) or a full URL (`…/v1/chat/completions`).

For native Ollama:

```bash
git config --global ez.provider ollama
git config --global ez.model installed-model:tag
git config --global ez.endpoint http://localhost:11434
```

The model must support tools. The `options` and `think` fields in `ez.requestOptions`
configure Ollama generation and reasoning; unknown options are rejected.
HTTPS is required outside loopback unless `ez.allowInsecureHttp=true`. TLS verification
remains enabled, using the configured CA bundle when provided. Redirects are not followed.

## Commands

```bash
# On the branch to rebase:
git ez rebase main

# On the branch that should receive feature:
git ez merge feature

# Explicit repository and final validation:
git ez rebase origin/main --repo /path/to/project --check python -B -m pytest

# Probe model compatibility without exposing the repository or shell tools:
git ez probe --output /tmp/ai-compatibility.json

# Read an existing report:
git ez show /path/to/project/.git/ez/runs/<id>

git ez --version
```

The target can be a branch, an already-fetched reference, a tag, a SHA, or an expression
such as `HEAD~2`. It is resolved and pinned at startup. The current repository is used by
default, including when invoked from a subdirectory. A checked-out branch and a clean
checkout are required.

Merge lets Git fast-forward or create a commit according to its configuration.
Rebase replays the current branch onto the target. This interface supports simple
integration, without the full set of Git options (interactive rebase, squash, octopus
merge, etc.). Git aliases are optional: `git config --global alias.ai-rebase 'ez rebase'`.

`--check` must be the last option. All following arguments form a command executed
without a shell at the target repository root. Its environment must provide the project's
dependencies. The agent can also run it while making corrections; it is run again after
integration. A nonzero exit code, timeout, or unexpected checkout change invalidates the
run. Test artifacts must be ignored or cleaned up by the target project.

The probe checks a text response, a native call to the harmless `compatibility_echo`
tool, and subsequent use of its result, through the same connectors used for resolution.
It does not modify a repository or execute model commands. A `compatible` result is not
a measure of conflict-resolution quality.

## Integration lifecycle

1. Check the checkout and pin the branch and target SHAs.
2. Run Git. If there are no conflicts, do not call the AI.
3. At each conflict stop, save the Git versions and open one agent session for all
   conflicts at that stop.
4. The agent explores, edits, and tests, then calls `finish` with its rationale and confidence.
5. Check files, Git metadata, and the confidence threshold; stage changes and continue
   Git. A conflict on a later commit opens a new session.
6. Record diffs, run optional validation, and check final invariants.
7. Preserve the audit and display its location.

The engine owns staging and Git continuation. It checks `MERGE_HEAD`, result ancestry,
and input references, among other invariants. Hooks, commit signing, rerere, and caller
Git settings are respected. If rerere reuses and stages a resolution, that decision is
recorded without an AI call. Hook and signing failures are not bypassed.

The Deep Agents runtime provides local tools, context management, and SQLite checkpoints.
Delegation is disabled. Offloaded history is stored separately from the repository.
The model receives explicit base/ours/theirs semantics for the Git strategy in use.
Tools execute on the host; their output is sent to the configured model.

Low confidence, an error, or an interruption leaves local state available for inspection.
There is no automatic rollback. `integrated` indicates that Git completed, whereas
`succeeded` also requires successful checks. Validation failure after integration
therefore preserves the local result with a failed run status. Failure to write a
required agent-session or final audit snapshot also fails the run, including failures
of the Git commands that capture diffs and enumerate files. Partial capture evidence
is retained and the failure is reported in the manifest and Markdown report.
A session snapshot failure prevents Git continuation; a final snapshot failure preserves any completed
integration. A secondary capture error does not replace the original failure reason.

## Audit

Traces are stored in the target repository's common Git directory:

```text
.git/ez/runs/<id>/
  run.json                   # Strategy, before/after SHAs, statuses, reused Git decisions
  report.md                  # Conflicts, decisions, reasons, and links to evidence
  config.json                # Effective configuration with redacted token
  system-prompt.txt
  integration.log            # Git commands and output
  result.diff                # Full before/after diff
  range-diff.txt             # Commit series comparison, rebase only
  rebase-todo.txt            # Initial native sequence, sequencer-backed rebases
  rebase-commits.log         # Result commits/messages, sequencer-backed rebases; truncation is explicit
  rebase-selection-anchor.txt # Internal exclusion object when needed; truncation is explicit
  conflicts/0001/
    base.txt / ours.txt / theirs.txt / marked.txt
    request.json             # Initial conflict evidence
    proposed.txt / resolution.diff
  sessions/0001/
    runtime.json / mission.json
    checkpoints.sqlite
    events.jsonl / messages.json / decision.json
    worktree.diff / staged.diff
    files.json / files/*.bin
  validation.stdout / validation.stderr
  exit/
    files.json / worktree.diff / files/*.bin
```

Files are created at the corresponding stage; an interrupted operation has no final
diff. Snapshots retain edits, new non-ignored files, and deletions, including after a
failure. Copies are limited to `ez.maxFileBytes`; truncation and read errors are reported.
Symlinks are not followed. The final capture also retains the effects of validation
that changed files or HEAD.

The `range-diff` compares pinned SHAs, but commit matching remains heuristic and does
not prove semantic equivalence. Checkpoints and events retain the execution trajectory;
`messages.json` is a potentially compacted snapshot. Traces remain local, without
LangSmith tracing, and may contain sensitive code or output.

## Limitations

Content checks apply to conflicted files and to supporting files changed by the agent.
Regular files integrated by Git without conflict are exempt from these checks when
their contents and permissions remain unchanged during resolution, including binary
and oversized resources. Audit file capture limits still apply.

Regular UTF-8 file conflicts are supported, including textual deletions and renames
coordinated by the agent. Binary conflicts, symlinks, submodules, file-mode conflicts, and
unrelated histories require manual handling. Rebasing merge commits is rejected.
Confidence scores are not calibrated, and passing tests do not prove semantic correctness.

The shell has the privileges of the host process: the repository is not a sandbox.
System and network isolation are the user's responsibility. Checks detect state changes;
they do not prevent or undo arbitrary commands. The engine remains noninteractive and
uses `diff3` markers. Environment variables that redirect the repository, index, or
objects are excluded to preserve the selected checkout.

After a failure, inspect the report and `git status`. An active operation can be
continued or aborted manually with Git:

```bash
git status
# If a rebase is still active, resolve and stage files before continuing:
git rebase --continue
# Or abandon that active rebase:
git rebase --abort
# For an active merge, use git merge --continue or git merge --abort instead.
```

If Git already completed and only final validation failed, there is no active operation
to continue or abort: inspect the resulting commit and validation output.
There is no agent session resume command:
checkpoints are for inspection. Pushing remains the user's decision.

## Development

```bash
uv sync --locked
uv run --locked pytest -v --cov --cov-report=term-missing
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy src
uv build
```

### Test suites and coverage

Every test has exactly one `unit` or `integration` marker; collection rejects missing
or overlapping categories. Unit tests isolate behavior without Git subprocesses,
model runtimes, or network services. Integration tests exercise real Git, SDKs,
the agent runtime, local HTTP/TLS, or CLI subprocesses. Both suites run explicitly
in every CI test matrix job.

```bash
# Fast isolated checks:
uv run --locked pytest -v -m unit

# Git, SDK/runtime, HTTP/TLS, and CLI integration:
uv run --locked pytest -v -m integration

# Combined line and branch coverage, including Python subprocesses:
uv run --locked pytest --cov --cov-report=term-missing \
  --cov-report=html:ci-reports/coverage-html \
  --cov-report=xml:ci-reports/coverage.xml
```

CI collects coverage during the unit step, appends integration coverage, and uploads
combined JSON/XML/HTML coverage reports plus separate JUnit results as artifacts for
each OS/Python combination. Open `coverage-html/index.html` from an artifact for
file-by-file detail. Reports are retained for 14 days, including when tests fail.
Coverage measures exercised code paths; it does not establish resolution correctness.
Critical tests assert committed file contents, reference ancestry, retained local state,
and recorded audit evidence. Controlled fault injection covers input-reference races,
audit write failures, and provider errors, including checkpoint redaction. Scripted
model replies validate orchestration and protocol behavior, not the quality of a real
model's decisions or every possible concurrent filesystem change.

The pytest suite covers real temporary Git repositories, SDKs, TLS, and the agent graph.
Installed-command tests use a deterministic local OpenAI-compatible HTTP server.
No automated test calls a remote model. CI covers Python 3.11 through 3.14 on
Linux and macOS. The workflow runs three independent job families: `quality`
(Ruff lint, formatting, and mypy on Ubuntu with Python 3.11), `tests`, and `packages`.
Both `tests` and `packages` cover the full OS/Python matrix. Each packaging job builds
the distributions and installs the wheel into a fresh environment outside the checkout
to verify Git discovery, command help, manual lookup, audit display, and conflict-free
merge/rebase without a model server.

Only pushes to `main` trigger branch CI; pushes to other branches and tags do not.
Pull requests targeting any branch trigger CI when opened, reopened, or updated.
Branches other than `main` therefore receive automatic checks through pull requests.
A newer run cancels an older run for the same workflow, event type, and pull request
or Git reference; push and pull-request runs do not cancel each other.

Run the same installation check locally after building:

```bash
uv run --locked python scripts/check_install.py dist/git_ez_ai-*.whl
```

```bash
# Deterministic fixture without a model or network:
uv run python examples/demo.py
uv run python examples/demo.py --operation merge

# The same scenario with the model configured in Git:
uv run python examples/demo.py --ai
```

The demo fixture combines a translation with name whitespace cleanup; it is not a
general-purpose resolver. It is injected for this example and is not a configurable mode.

The [engine](src/git_ez/engine.py) owns the Git lifecycle. The
[resolver](src/git_ez/agent.py) and its [runtime](src/git_ez/agent_runtime.py) manage the
agent; [checks](src/git_ez/checks.py), [connectors](src/git_ez/llm.py), and
[audit](src/git_ez/audit.py) remain separate. Tests inject deterministic resolvers to
verify orchestration without a server. These internal interfaces are not a plugin API.

Exit codes: `0` = successful local integration, displayed report, or compatible probe;
`1` = failed run/probe; `2` = configuration, CLI, or precondition error.

## License

Copyright (c) 2026 git-ez contributors.

Licensed under the GNU General Public License, version 3 only
(`GPL-3.0-only`). See [LICENSE](LICENSE) for the full terms. This license covers
git-ez; it does not change the license of repositories processed by the tool.
Third-party dependencies retain their own licenses. See the
[dependency licensing review](docs/licensing.md) for the current scope and open item.

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
