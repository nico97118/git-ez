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
  rebase-todo.txt.sha256     # Sequence fingerprint written before replay
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

The manifest separates the execution outcome from audit capture:

- `status` and `integrated` describe the run and whether Git completed the integration.
- `failure` records the primary error's phase, type, and message. `error` retains
  the human-readable reason. Secondary capture errors appear in `audit_errors`
  without replacing that reason.
- `validation.status` is `not_requested`, `not_run`, `running`, `passed`, `failed`,
  `timed_out`, or `interrupted`. A successful command can coexist with a failed run
  if its output could not be saved or a later invariant check failed.
- `evidence` records availability for worktree captures, final session messages and
  events, validation output, rebase-sequence verification, and final summaries.
  `available` means that capture succeeded; `partial` means a capture is truncated,
  contains unavailable files, or an existing artifact could not be finalized;
  `unavailable` means no usable artifact could be confirmed for that capture. This is not an
  exhaustive inventory of every audit file. A journal stays partial after a failed
  append even when later events can be saved. Failure to retain an ordinary model
  or tool event stops the agent as an audit failure, rather than reporting a model
  service failure. Error-event capture does not replace an existing failure.

Final captures are attempted independently. Losing a conversation file does not
prevent capturing the worktree or attempting the other summaries. If the report
or manifest cannot be finalized, the CLI still prints the execution outcome,
secondary audit errors, and the audit location. Existing files may be older or
incomplete snapshots; consult the CLI's availability warning before trusting them.
Handled initialization failures also mark the session as failed, even when its
folder or runtime metadata could not be prepared and no model was called. The
remaining captures are still attempted independently.
The session's terminal event includes the outcome of session metadata capture;
final run summaries and validation still have their own outcomes. Unexpected
exceptions are re-raised unchanged after cleanup. The audit records their type
and failed/interrupted status without copying arbitrary exception messages.
No write durability across power loss or automatic recovery is promised.

The `range-diff` compares pinned SHAs, but commit matching remains heuristic and does
not prove semantic equivalence. Events and agent files retain execution evidence;
`messages.json` is a potentially compacted snapshot. Traces remain local, without
LangSmith tracing, and may contain sensitive code or output.
