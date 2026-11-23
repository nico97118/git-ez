# Integration lifecycle and audit

[README](../README.md) · [Workflow policies](workflows.md)

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

The Deep Agents runtime provides local tools, context management, and in-memory
graph checkpoints. Session `runtime.json` records dependency versions and the
checkpoint storage mode. No graph checkpoints are saved for inspection or restart.
Model/tool events are written progressively to `events.jsonl`; offloaded history
and agent files are written directly to the session's `agent-files/` directory.
These artifacts survive independently of graph state. They do not represent every
internal graph state or pending task. Abrupt process termination can leave an
incomplete event or file and skips final messages, worktree snapshots, and status
updates; inspect the artifacts already written and the actual Git state.
Delegation is disabled. Offloaded history is stored separately from the repository.
The model receives explicit base/ours/theirs semantics for the Git strategy in use.
Tools execute on the host; their output is sent to the configured model.

Low confidence, an error, or an interruption leaves local state available for inspection.
There is no automatic rollback. The CLI's `Integrated` field indicates known Git
completion; schema-2 manifests record this as `git.integration=completed`. Overall
`succeeded` also requires successful checks and publication of the terminal manifest.
Validation failure after integration
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
    agent-files/             # Agent files and offloaded history, outside the worktree
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

New runs use audit schema 2. `run.json` is the authoritative, revisioned snapshot;
`report.md` is a derived cache. The manifest separates execution from evidence:

- `lifecycle` identifies preparation, active execution and finalization. `outcome`
  is `pending`, `succeeded`, `failed` or `interrupted`. A saved `pending` snapshot
  is the last recorded state, not evidence that a process is still running.
- `request`, `inputs` and `repository` retain effective policy, pinned references,
  configuration with redacted credentials, and checkout provenance.
- `git.integration` distinguishes `not_started`, `in_progress`, `completed`,
  `not_completed` and `unknown`. Native return facts, before/after observations and
  the known result OID survive later capture or validation failures. Unknown
  completion is never inferred as success from current ancestry.
- `checks.validation` records whether validation was requested, started, passed,
  failed, timed out or interrupted. Its actual result is retained even if stdout or
  stderr cannot be stored. Final invariant checks are recorded separately.
- `primary_failure` and `primary_failure_id` identify the causal failure. `issues`
  retain safe types, phases and secondary audit problems without replacing it.
- `requirements` and `artifacts` identify required/optional evidence, capture IDs,
  retained lengths and SHA-256 hashes, append prefixes, and explicit capture
  limitations. `audit_health` is `complete`, `degraded` or `incomplete`.
- Session execution and evidence health are separate: `resolved` does not imply
  that its required evidence was retained or that Git continued. Proposed,
  accepted and actually staged changes remain distinct.

A missing required proposal or native-step capture prevents the next Git mutation.
A final evidence failure preserves any completed integration but fails the run.
An intentionally bounded file prefix is distinguished from an unexpected read/write
failure. Never-produced outputs, such as stdout from an unstarted validation,
are marked inapplicable rather than presented as lost captured output.
Ordinary model/tool events are required. After a failed append, the stream is sealed
at its last acknowledged prefix; later finalization cannot make a missing event
complete. Unindexed trailing bytes remain raw, unverified evidence.

Independent final captures are attempted even after a primary error. A failed terminal
publication produces a nonzero command result and a warning that saved state may be
older. Git is never rolled back. If initialization cannot create an owned directory,
the CLI identifies the intended location without overwriting another run.

Compacted `messages.json` and duplicate runtime/decision exports are optional when
canonical events and facts exist. Their loss is a visible warning. Markdown rendering
and cache publication occur after the core manifest is committed; a presentation-only
failure does not change that committed outcome. The cache identifies its source run,
revision and manifest hash, but its existence is not proof of current success.

`git ez show RUN_DIRECTORY` reads and validates the manifest and required evidence,
then renders a fresh view without executing Git, loading a model, or modifying files.
A stale Markdown cache cannot override an incomplete or unreadable manifest. Missing,
changed or unsafe mandatory evidence produces an unverified view that separates the
recorded historical result from current evidence availability. Reading an audit does
not recapture the repository, repair it, or establish process liveness.

For integration commands, exit 0 requires published core success; exit 1 means an
admitted run failed, was interrupted, or could not publish its terminal state. Exit 2
indicates a pre-admission/configuration/usage error. Unexpected execution exceptions
and `SystemExit` retain their propagation semantics after best-effort cleanup.
For `show`, exit 0 means a valid terminal audit whose mandatory evidence meets policy
(including a correctly recorded failed run); exit 1 means a displayable but unfinished,
unverifiable audit; exit 2 means no trustworthy manifest view is available.
Only schema 2 is supported. Unsupported schemas or malformed/missing manifests never
fall back to trusting an old success report. Pure-rendering failure uses a minimal
structured text view.

Publication atomically replaces one manifest on a local filesystem after its capture
files have been written. Captures are not overwritten with another snapshot. Stream
prefixes and bounded file copies are hashed; no symlinks are followed when reading
source or audit files. Checksums detect inconsistency, not malicious rewriting by a
host-privileged process. Captures are observations over an interval, not transactional
filesystem snapshots. No power-loss durability or automatic recovery is promised.
Numeric global time/token/disk/output budgets and retention policies are unchanged.

The `range-diff` compares pinned SHAs, but commit matching remains heuristic and does
not prove semantic equivalence. Events and agent files retain execution evidence;
`messages.json` is a potentially compacted snapshot. Traces remain local, without
LangSmith tracing, and may contain sensitive code or output.
