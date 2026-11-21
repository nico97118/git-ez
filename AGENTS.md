# Working on git-ez

## Purpose and scope

`git-ez` is a Git extension: users run `git ez rebase <target>` or
`git ez merge <target>` to perform a local integration with LLM-assisted conflict
resolution and an inspectable audit of changes, decisions, and reasons.

- The Python distribution is `git-ez-ai`, the import package is `git_ez`, and the
  executable is `git-ez` (invoked as `git ez`). Keep these names distinct when
  updating packaging, installation instructions, and manual search paths.
- Keep the tool generic across supported LLM providers, including internal servers.
- Use native Git configuration under `ez.*`, respecting Git scopes and includes.
- Operate on the user's current checkout and branch. Do not add automatic fetch,
  push, backup branches, or worktree creation.
- Invoke the LLM only when conflicts require resolution.
- Keep the supported command subset explicit in the README and manual. Do not
  imply that arbitrary native Git flags are forwarded or that every Git workflow
  is supported. Document intentional execution-policy overrides.
- Preserve Git behavior where supported, including hooks, signing, and rerere.
  Document unsupported cases rather than silently changing their meaning.
- Leave the actual local state available for inspection on failure or interruption;
  do not introduce automatic rollback or claim success after incomplete validation.

These are product contracts. If a requested change conflicts with them, explain the
consequence and clarify the intended change before implementing it.

## Code map

- `src/git_ez/cli.py`: command parsing and user-facing results.
- `config.py`: native Git configuration and redacted configuration output.
- `options.py`: explicitly supported native Git options and argument conversion.
- `git.py`, `engine.py`, `checks.py`: Git execution, integration lifecycle, and guards.
- `conflicts.py`: conflict evidence and resolution decisions.
- `agent.py`, `agent_runtime.py`, `llm.py`: resolver contract, agent runtime, providers.
- `audit.py`: persistent evidence and reports.
- `probe.py`: provider connectivity diagnostics.
- `tests/`: pytest tests, temporary repositories, deterministic resolvers, and local
  HTTP/TLS fixtures.
- `examples/`: demonstrations and sample Git configuration.
- `docs/man/git-ez.1`: the packaged Git manual.
- `scripts/check_install.py`: isolated wheel installation and Git command checks.

Module names above are relative to `src/git_ez/`. Read nearby code and tests before
changing a behavior. Keep this map accurate when moving responsibilities.

## Implementation principles

- Prefer the existing implementation and the smallest coherent change that meets
  the task. Add abstractions or dependencies only for a concrete current need;
  explain their purpose. Avoid speculative extension points and unrelated cleanup.
- Use Python 3.11-compatible code, type annotations, and the existing strict mypy
  configuration. Ruff defines formatting and lint conventions, including 100-column
  lines. Use pytest functions and fixtures for tests.
- Project-owned code is licensed under `GPL-3.0-only`. Preserve SPDX notices and
  review license compatibility when adding or updating dependencies. Keep third-party
  licensing concerns in `docs/licensing.md`; do not relabel third-party code.
- Manage dependencies with uv. Update `pyproject.toml` and `uv.lock` together when
  dependencies change. Keep unrelated dependency versions unchanged.
- Keep Git orchestration in the engine. The resolver proposes file changes and
  explains decisions; the engine owns staging and integration continuation.
- Preserve checks on branch identity, pinned references, operation state, and final
  ancestry. A model's confidence or a successful tool call does not prove that the
  requested Git operation succeeded.
- Raise actionable errors, preserve relevant exception causes, and report what
  actually happened. Distinguish completed integration from overall run success.
- Update user documentation and examples when CLI behavior, configuration, or audit
  output changes. Do not promise capabilities that the implementation lacks.

## Audit and credentials

Traceability is part of the feature, including unsuccessful runs.

- Preserve conflict inputs, proposed and applied changes, decisions and reasons,
  validation results, and final repository state through the existing audit flow.
- Make unavailable evidence, capture limits, and truncation explicit. Do not present
  a partial capture as complete or follow symlinks while capturing file contents.
- Keep credentials out of logs, reports, committed fixtures, and model tool
  environments. Preserve configuration redaction and token filtering.
- Agent shell tools run with host-process privileges. Do not describe their
  execution as an operating-system sandbox.

## Development and validation

Set up the environment with:

```sh
uv sync --locked
```

Run focused tests while working, for example:

```sh
uv run --locked pytest tests/test_engine.py -k merge
```

Before every code-changing commit, run all of:

```sh
uv run --locked pytest -v --cov --cov-report=term-missing
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy src
```

Fix failures before committing. Reuse successful results while their relevant
inputs remain unchanged. For documentation-only commits, check the documentation
and its examples; Python checks are not required unless executable behavior changes.
If a required check cannot run, report the blocker and do not silently bypass it.

For packaging, dependency, or executable-entry-point changes, also run `uv build`
and run `uv run --locked python scripts/check_install.py dist/git_ez_ai-*.whl` to verify
the installed command and manual outside the source checkout. A `man` viewer is
required. CI covers Python 3.11 through 3.14 on Linux and macOS; consider that range
when changing compatibility.

- Mark every test with exactly one of `unit` or `integration`; use the existing
  module markers where applicable. Unit tests do not start Git, model runtimes, or
  network services. Keep Git/SDK/runtime/CLI scenarios in the integration selection.
- Use pytest-cov with branch and subprocess measurement. CI runs both categories
  and combines their coverage; do not judge whole-project coverage from unit tests
  alone or omit production code just to improve a percentage.
- Test observable behavior and regressions; avoid tests that merely mirror code.
- Reuse existing fixtures and helpers. Use temporary Git repositories with isolated
  Git configuration, deterministic resolvers, and local HTTP/TLS servers as needed.
- Automated tests must not call external LLM services, require real credentials,
  or modify a user's repository or global Git configuration.
- For Git lifecycle and audit changes, cover relevant success and failure paths,
  repository state, and recorded evidence. Use real Git in temporary repositories
  where Git semantics are the behavior under test.

## Collaboration and Git workflow

- Use English for repository documentation, code comments, docstrings, CLI messages,
  generated report labels, and commit messages. Communicate with the maintainer in French.
  Non-English text may remain in test or demo data when relevant to the scenario.
- Proceed autonomously with changes, tests, and coherent commits within the task.
  Push only when explicitly requested. This development policy does not add push
  behavior to the git-ez product.
- Inspect the working tree before editing or committing. Preserve unrelated user
  changes and stage only changes belonging to the task.
- Do not rewrite history or discard user work without explicit authorization.
- Explain the result, relevant validation, and remaining limitations. Write commit
  messages and repository documentation for readers who have not seen the chat.


## Commit conventions

Use this Conventional Commits format for new commits:

```text
<type>[(scope)][!]: <imperative summary>
```

- Write the subject in English, starting with a lowercase imperative verb, without
  a trailing period. Keep it within 72 characters when practical.
- Choose the type by the purpose of the change:
  - `feat`: add user-visible behavior.
  - `fix`: correct faulty behavior.
  - `refactor`: restructure code without changing its observable behavior.
  - `test`: add or improve tests without changing production behavior.
  - `docs`: change documentation only.
  - `build`: change packaging, dependencies, or build tooling.
  - `ci`: change CI workflows or automation.
  - `perf`: improve performance without changing intended behavior.
  - `chore`: maintenance that does not fit a more specific type.
- A scope is optional. Use a short, concrete component name when it improves clarity,
  such as `cli`, `git`, `agent`, `config`, or `audit`; omit it for cross-cutting changes.
- Mark incompatible user-facing changes with `!` and a `BREAKING CHANGE:` body entry
  explaining the impact and migration. Do not introduce compatibility shims unless
  the task requires them.
- Add a body when the reason or tradeoff is not obvious. Explain the problem,
  resulting behavior, and relevant validation for a reader without the chat history.

Examples:

```text
fix(audit): fail the run when a required snapshot cannot be written
feat(cli): display the latest integration report
refactor(config): centralize Git setting conversion
test(git): cover reference changes during final verification
ci: preserve test and coverage reports
```

## Commit boundaries

Each commit must have one precise objective and leave the project in a working,
reviewable state. A functional commit may be a fix, test improvement, documentation
change, or build change; it does not have to introduce a feature.

- Keep implementation, regression tests, documentation, and dependency/lockfile
  updates together when they are needed to complete the same objective. Choose
  `feat` or `fix` for such a commit, rather than splitting out its required tests.
- Split independent objectives into separate commits. Organize by resulting behavior
  or purpose, rather than by file or technical layer. Avoid unrelated cleanup.
- Every code-changing commit must pass the validation required above. Do not leave
  failing tests, broken imports, or incomplete migrations for a later commit to fix.
- Before committing, review the staged diff and state its objective in one sentence.
  If that requires listing unrelated outcomes, split the changes. Stage only the
  intended files and keep the change coherent for review and reversion.
- Use descriptive subjects instead of messages such as `WIP`, `misc fixes`, or
  `address feedback`. Temporary work must not remain as incomplete commits in the
  delivered history; existing history still requires explicit authorization to rewrite.
