# Conflict resolution journal

[README](../README.md) · [Workflow policies](workflows.md)

## Purpose

The audit records actions taken by git-ez on top of Git. Git remains the source
for commit history, native command behavior and repository state: use `git log`,
`git diff` and `git status` to inspect those.

For each conflict sent to the resolver, the journal retains:

- The conflict diff (`ours` versus `theirs`) and original inputs, including markers.
- The proposed resolution, its diff against `ours`, and whether it was applied.
- The LLM's explanation of the resolution.
- The LLM's confidence score.

`ours` and `theirs` follow the Git strategy: during rebase, `ours` is the new base
and `theirs` is the commit being replayed. A single resolver session can handle
several files; its explanation and confidence are attached to each conflict.
Confidence is a model estimate, not independent validation of correctness.

## Files and inspection

Runs are stored under `ez/runs/<id>` in the repository's common Git directory:

```text
run.json                      # Run status and per-conflict decisions (schema 3)
report.md                     # Human-readable derived report
conflicts/0001/
  request.json                # Base, ours, theirs and initial marked content
  conflict.diff               # Ours versus theirs
  resolution.txt              # Retained proposal (empty for deletion)
  resolution.diff             # Ours versus retained proposal
sessions/0001/                # Model/tool events, messages and agent files
proposals/0001/               # Interactive review attempts, when requested
validation.stdout / validation.stderr  # Optional --check output
```

Files appear when their execution stage is reached. `run.json` distinguishes a
file deletion from an empty file, a proposal from an applied resolution, and a
completed integration from overall run success. Interactive feedback, acceptance
and skipped proposals are recorded separately. Native rerere reuse needs no LLM
call and produces no fabricated model decision.

Use `git ez show RUN_DIRECTORY` to render the saved journal read-only, without
Git or model calls. It reads `run.json`, ignoring a stale `report.md`. It displays
recorded facts; it does not certify files or reconstruct Git history. Only schema 3
is supported; inspect older or malformed journals directly.

Session events and `agent-files/` are retained progressively as supporting
diagnostics. Their export failures are reported as warnings. They are not a
complete model graph history, a process-liveness signal or a recovery checkpoint.
Rebase sequence files may also be present: the engine uses them to check that
native autosquash instructions were captured before replay, without editing them.

## Failure and limits

The engine checks the branch, pinned references, operation state and final ancestry.
It owns staging and Git continuation. Hooks, signing and rerere retain their native
behavior. These execution checks do not require archiving Git's own history.

Conflict inputs and retained proposals must be saved before staging. A failure to
retain them stops continuation. Unsupported or unreadable inputs are reported
without claiming a complete capture. Proposals are bounded by `ez.maxFileBytes`;
initial conflict inputs have a 10 MB safety limit. File reads do not follow symlinks.
If the resolver fails or is interrupted before returning a decision, available
edits are retained when possible, with no invented explanation or confidence.

An error or interruption leaves actual local state available for inspection.
There is no automatic rollback. If integration completed before validation failed,
its local commits remain. A failure to save the final journal returns a nonzero
result and warns that the saved status may be older. Markdown rendering failure
alone is a presentation warning. Validation output loss is a diagnostic warning;
the validation result remains recorded independently.

Known configured credentials are filtered from saved content. Audit files can
still contain sensitive source code and command output; review them before sharing.
Agent tools run with host-process privileges, without an operating-system sandbox.

For integration commands, exit `0` requires successful integration, checks and final
journal publication; `1` indicates failure or interruption, and `2` indicates a
configuration, usage or precondition error. For `show`, exit `0` means the journal
was readable, including a recorded failed or unfinished run; `2` means it could not
be read. Display success does not validate the recorded resolution.
