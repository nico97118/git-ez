# Contributing to git-ez

For installation, configuration, and usage, see the [README](README.md).
Read [AGENTS.md](AGENTS.md) for the project contracts and detailed development rules.

## Development setup

Requires Python 3.11+, Git 2.38+, and uv. From a local checkout:

```bash
uv sync --locked
```

## Required checks

Before every code-changing commit, run:

```bash
uv run --locked pytest -v --cov --cov-report=term-missing
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy src
```

For documentation-only changes, check the documentation and its examples; Python
checks are only required when executable behavior changes.

## Test suites and coverage

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

## Packaging checks

For packaging, dependency, or executable-entry-point changes, also build and run
the installation check. A working `man` viewer is required.

```bash
uv build
uv run --locked python scripts/check_install.py dist/git_ez_ai-*.whl
```

## Demo scenarios

```bash
# Deterministic fixture without a model or network:
uv run --locked python examples/demo.py
uv run --locked python examples/demo.py --operation merge

# The same scenario with the model configured in Git:
uv run --locked python examples/demo.py --ai
```

The demo fixture combines a translation with name whitespace cleanup; it is not a
general-purpose resolver. It is injected for this example and is not a configurable mode.

## Code organization

The [engine](src/git_ez/engine.py) owns the Git lifecycle. The
[resolver](src/git_ez/agent.py) and its [runtime](src/git_ez/agent_runtime.py) manage the
agent; [checks](src/git_ez/checks.py), [connectors](src/git_ez/llm.py), and
[audit](src/git_ez/audit.py) remain separate. Tests inject deterministic resolvers to
verify orchestration without a server. These internal interfaces are not a plugin API.

## Submitting changes

Keep changes focused on one objective, with regression tests and documentation
when behavior changes. Preserve unrelated work and do not rewrite existing history.
Use English for documentation, code comments, docstrings, CLI messages, report
labels, and commit messages.

Use Conventional Commits: `<type>[(scope)][!]: <imperative summary>`, with a
lowercase English imperative and no trailing period. For example:
`docs: move development guidance into contributing guide`.
Keep each commit working and reviewable, and describe the resulting behavior and
relevant validation in the pull request. See [AGENTS.md](AGENTS.md) for the full
commit conventions and validation requirements.
