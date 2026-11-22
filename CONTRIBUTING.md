# Contributing to git-ez

For installation, configuration, and usage, see the [README](README.md).
Read [AGENTS.md](AGENTS.md) for the project contracts and detailed development rules.

## Development setup

Requires Python 3.11+, Git 2.38+, and uv. From a local checkout:

```bash
uv sync --locked
```

The default `dev` group includes `test` (pytest and its helpers) and `lint`
(Ruff and mypy). CI selects the tools needed for each responsibility:

| Job | Dependency selection | Validation |
| --- | --- | --- |
| Quality (Ubuntu) | Runtime + `lint` | Lint, formatting, and strict typing |
| Tests (Linux/macOS/FreeBSD) | Runtime + `test` | Functional suites and coverage |
| Packages (Linux/macOS) | Runtime only | Build and installed-wheel checks |

FreeBSD combines functional and installed-wheel validation in its native VM job.
The quality job validates development guardrails; the test and packaging jobs
establish behavior in the supported usage environments. Ruff and mypy are not
required to install or use git-ez.

### FreeBSD

Install the system Python, its SQLite module, and the native build toolchain.
For the full development environment, GNU Make is also needed to build Ruff's
bundled jemalloc:

```sh
pkg install git uv python312 py312-sqlite3 rust pkgconf gmake
uv sync --locked --python /usr/local/bin/python3.12
```

For Python 3.11, use `python311`, `py311-sqlite3`, and `/usr/local/bin/python3.11`.
System Python packages do not need to be exposed to the virtual environment.

**Ruff warning:** on the tested FreeBSD 16.0-CURRENT arm64 VM, Ruff 0.17.6
intermittently aborts in jemalloc with multiple threads. Run it with one Rayon
thread; the lint and formatting rules remain the same:

```sh
RAYON_NUM_THREADS=1 uv run --locked ruff check .
RAYON_NUM_THREADS=1 uv run --locked ruff format --check .
```

Ruff and mypy are development tools, absent from the runtime dependencies.
The dedicated FreeBSD CI validates installation and usage: it installs the `test`
dependency group, runs the complete pytest suite with branch and subprocess
coverage, builds distributions, and checks an installed wheel outside the checkout
(Git discovery, command help, manual, audit display, merge, and rebase).
It does not install or run Ruff or mypy, and does not require GNU Make for Ruff.
The existing Ubuntu quality job keeps running lint, formatting, and type checks.

The FreeBSD job persists uv's compiled-wheel cache through GitHub Actions.
`.uv-cache/` lives in the workspace synchronized to the VM and copied back to the
runner. Cache keys separate FreeBSD releases, architectures, and Python minor
versions, and include `uv.lock`; a fallback key reuses compatible cache entries
when the lockfile changes. uv still resolves the selected locked dependencies
and validates wheel compatibility. The virtual environment is always recreated.
At the end of a successful run, `uv cache prune --ci` retains wheels built from
source while discarding downloaded wheels and unpacked build sources. GitHub
saves the resulting cache after the job succeeds; the first run or an evicted
cache still requires compilation. This cache is separate from the action's VM
image cache and does not cache installed system packages.

To reproduce the FreeBSD deployment/test environment:

```sh
uv sync --locked --no-default-groups --group test --python /usr/local/bin/python3.12
uv run --locked --no-default-groups --group test pytest -v --cov --cov-report=term-missing
uv build --python .venv/bin/python
uv run --locked --no-default-groups python scripts/check_install.py dist/git_ez_ai-*.whl
```

Select `--no-default-groups` and the appropriate `--group` on each `uv run` as
well as `uv sync`. `--locked` alone would synchronize the default development
group again. Packaging commands omit `--group` to select runtime dependencies.
FreeBSD CI targets 15.1 amd64/Python 3.12 only. ARM64 emulation is excluded from
the PR, scheduled, and manual jobs after a cold dependency build exceeded the
90-minute limit before reaching the tests.
The in-memory checkpoint experiment passed all 554 tests on macOS. Focused
native validation passed 134 agent/CLI tests and isolated installed-wheel/manual
checks on FreeBSD 16.0-CURRENT arm64 with Python 3.12.14. This is not a complete
FreeBSD suite or a validation of the 15.1 amd64 CI target. A separate abrupt
process-exit probe confirmed that agent files and completed tool events survive
without final cleanup; final messages and session status are unavailable in that
case. Models and HTTP/TLS servers used by the tests are deterministic and local;
no external LLM is called.

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
the agent runtime, local HTTP/TLS, or CLI subprocesses. Every CI test environment
runs both categories. Linux/macOS jobs invoke them separately; FreeBSD runs the
complete suite in one invocation.

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

Linux/macOS CI collects coverage during the unit step, appends integration coverage,
and uploads combined JSON/XML/HTML coverage reports plus separate JUnit results as artifacts for
each OS/Python combination. Open `coverage-html/index.html` from an artifact for
file-by-file detail. Reports are retained for 14 days, including when tests fail.
FreeBSD uploads combined coverage XML and a JUnit report for the complete suite.
Coverage measures exercised code paths; it does not establish resolution correctness.
Critical tests assert committed file contents, reference ancestry, retained local state,
and recorded audit evidence. Controlled fault injection covers input-reference races,
audit write failures, and provider errors, including error redaction.
Finalization tests combine primary failures or interruption with secondary errors
in error callbacks, session messages/events, interactive proposal capture, validation output, and
final report/manifest writes. They verify error precedence, evidence availability,
CLI fallback diagnostics, branch identity, and whether Git remains active. Scripted
model replies validate orchestration and protocol behavior, not the quality of a real
model's decisions or every possible concurrent filesystem change.

The pytest suite covers real temporary Git repositories, SDKs, TLS, and the agent graph.
Installed-command tests use a deterministic local OpenAI-compatible HTTP server.
No automated test calls a remote model. CI covers Python 3.11 through 3.14 on
Linux and macOS. A single `ci.yml` workflow runs four independent jobs or job
families: `quality` (Ruff lint, formatting, and mypy on Ubuntu with Python 3.11),
`tests` and `packages` for Linux/macOS, and `freebsd` for FreeBSD 15.1 amd64/Python
3.12. The FreeBSD job shares one VM and dependency cache for the complete test
suite and installed-package checks, without installing quality tools. All jobs
share triggers, read-only repository permissions, and cancellation policy.
The Linux/macOS matrices depend on the triggering event:

| Event | Test environments | Packaging environments |
| --- | --- | --- |
| Pull request or push to `main` | Linux/Python 3.11 and 3.14; macOS/Python 3.14 | Linux/Python 3.11; macOS/Python 3.14 |
| Weekly, version tag, or manual run | Linux and macOS, each with Python 3.11–3.14 | The same full matrix |

FreeBSD runs its single environment on every event listed above, including version
tags. Every selected test environment runs the complete unit and integration suites with
coverage; only the environment combinations are reduced. Intermediate Python versions
and macOS/Python 3.11 are checked by full runs, so incompatibilities specific to those
combinations can be discovered after a PR is merged.

Each packaging job builds
the distributions and installs the wheel into a fresh environment outside the checkout
to verify Git discovery, command help, manual lookup, audit display, and conflict-free
merge/rebase without a model server.

Only pushes to `main` trigger branch CI; pushes to other branches do not.
Pull requests targeting any branch trigger CI when opened, reopened, or updated.
Branches other than `main` therefore receive automatic checks through pull requests.
Full runs execute every Monday at 04:17 UTC on the default branch, on pushes of tags
matching `v*`, and through **Run workflow** (`workflow_dispatch`) on a selected ref.
Use a manual full run before merging changes that need broader compatibility checks.
Version tags trigger validation but do not publish a package; check the tag's full
run before publishing its release. Publishing a GitHub release does not trigger a
duplicate run.
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
