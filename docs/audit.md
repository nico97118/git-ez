# Resolution journals

[README](../README.md) · [Workflow policies](workflows.md)

## Purpose and storage

Every admitted merge or rebase creates a local journal, including runs without
conflicts and unsuccessful runs. Argument, configuration and precondition errors
can occur before a run is admitted. No option is needed to enable recording.

Journals live under `ez/runs/<id>` in the repository's common Git directory,
usually `.git/ez/runs/<id>`. They are outside versioned content. Linked worktrees
share this location; each run identifies its checkout. No automatic cleanup runs.
The integration prints its identifier and a `git ez audit show <id>` command.

The journal describes git-ez interventions. Use Git for commit history, native
integration behavior and repository diffs. Native commands executed by the engine
are not archived. A Git command requested by the LLM is an agent action and is
recorded like any other model-requested command.

## Inspecting a run

```sh
git ez audit list                  # All readable runs, newest first
git ez audit show                  # Most recently started run
git ez audit show RUN_ID           # Full identifier or unique prefix
git ez audit show RUN_ID --verbose # Include model/tool events and outputs
git ez audit show --path /path/to/copied-run
```

`list` includes date, operation, branch, status and checkout. An ambiguous prefix
is refused. `--path` works outside a repository and cannot be combined with an
identifier. `git ez audit` displays help. The former `git ez show` command is removed.

`show` presents recorded status, integration completion, conflicts, attempts,
explanations, confidence, decisions, diffs and validation results. Each reprompt
creates another attempt: earlier proposals and user feedback remain available.
Every attempt's conflict diff uses the original `ours` as its reference, allowing
comparison across revisions. During rebase, `ours` is the new base and `theirs`
is the commit being replayed. An explanation/confidence shared by several files
is identified as applying to that group. Deletion is distinct from empty content.

Acceptance and application are separate. A stopped or failed revision does not
apply an earlier proposal automatically. Native rerere reuse needs no LLM call
and produces no invented explanation or confidence.

`--verbose` adds the saved requests, responses, tools and shell invocations in
recorded order. Shell records include command, execution directory, duration,
exit code, stdout/stderr when available, timeout and output truncation. A script
is recorded as one invocation; subprocesses are not individually traced. Model
responses include usage metadata when supplied by the provider. The environment
and HTTP authorization headers are not dumped.

In a terminal, consultation uses Git pager conventions (`GIT_PAGER`, `core.pager`,
`pager.ez`). Pipes and redirections receive plain output without starting a pager.
Use `git --no-pager ez audit show` for direct display or `git --paginate ez audit
show` to request Git's pager. Quitting the pager is normal. The display adds no
arbitrary truncation; any capture limitations are identified separately.

## Files and failure handling

New journals use schema 4. `run.json` contains run metadata, conflict inputs'
locations, attempts and decisions. Conflict directories retain `request.json`
(base/ours/theirs/marked) and `conflict.diff`. Each proposal directory retains its
conflict resolutions, diffs and selected agent-changed files. Session directories
hold model/tool events, messages and `agent-files/`. Optional `--check` output is
in `validation.stdout` and `validation.stderr`. `report.md` is a derived convenience
copy: `show` reads the journal and saved captures, ignoring that cache.

Reads do not recapture repository state or load a model. They do not certify
files, repair journals or reconstruct history. Unsupported or malformed journals
receive raw-file guidance; no migration of older schemas is provided.

The engine retains its checks on branch identity, pinned inputs, operation state
and final ancestry. It owns staging and continuation. Conflict proposals and their
decisions must be saved before staging; a required write failure stops continuation.
Supporting event and validation-output export failures produce partial-journal warnings;
the validation result remains distinct from output availability. Unavailable
captures and truncations are explicit. Proposals are bounded by `ez.maxFileBytes`;
initial conflict inputs have a 10 MB safety limit. Content reads do not follow
symlinks. Known configured credentials are filtered, but source code and output
can still be sensitive: review traces before sharing.

Failure leaves actual local state available without rollback. If integration
completed before validation failed, its commits remain. A final journal save
failure returns nonzero and warns that saved state may be older. Markdown failure
alone is a presentation warning. After interruption, no missing explanation or
confidence is invented. A record left running is unfinished, not proof of a live
process. Events and agent files are not a complete graph history or crash recovery.

For integrations, exit 0 requires successful integration, checks and publication;
exit 1 means failure/interruption and exit 2 means a usage/configuration/precondition
error. Consultation returns 0 for a readable journal, including a recorded failed
run, and 2 for a selection or reading error. An empty list is successful.
