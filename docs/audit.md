# Conflict attempts and decisions

[README](../README.md) · [Workflow policies](workflows.md)

Every admitted integration writes a journal in `ez/runs/<id>` in the repository's
common Git directory, including failures and runs without model calls.

Use `git ez show RUN_DIRECTORY` to display a saved run. It reads saved records,
not current repository state, and ignores stale Markdown caches. New runs use
schema 4. Inspect older or malformed journals directly; no migration is provided.

Each conflict retains base/ours/theirs, marked input and its initial diff.
During rebase, ours is the new base and theirs is the replayed commit. Every
reprompt creates another attempt with its own proposed content, diff, explanation,
confidence and user feedback. Diffs compare each proposal to the original ours.
Explanation and confidence may be shared by several conflicts; that scope is
explicit. Deletion is distinct from empty content. Confidence is a model estimate.

Acceptance and application are separate. A failed revision never applies an older
proposal automatically. Both successful and failed attempts retain selected
supporting files changed by the agent, subject to capture limits. Missing or
truncated captures are explicit. A failure before a returned decision does not
invent an explanation or confidence. Model/tool events and agent files are
supporting diagnostics, not a complete graph history or crash recovery.

Git provides native history and repository-wide diffs. Engine commands are not
archived. Branch, pinned-input, operation-state and ancestry checks remain in the
engine. Required conflict proposals are saved before staging. Failed diagnostic
exports warn without changing a successful validation result. Failure leaves the
actual local state without rollback. Completed integration and overall success
remain distinct; a final journal publication failure returns nonzero.

Known configured credentials are filtered. File reads do not follow symlinks.
Source code and outputs can still be sensitive; inspect them before sharing.

Session `events.jsonl` records model requests/responses and tool actions in order.
Shell invocations retain command, working directory, timing, exit code, available
stdout/stderr and capture truncation. SDK timeouts require the timeout return code,
not merely a matching string in successful output. Scripts are single invocations;
subprocesses are not individually traced. Model usage metadata is retained when
provided. Credentials and the full process environment are not dumped. Events are
written progressively, independently of in-memory graph checkpoints.
