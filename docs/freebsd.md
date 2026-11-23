# FreeBSD installation (experimental)

[README](../README.md) · [Development on FreeBSD](../CONTRIBUTING.md#freebsd)

FreeBSD CI targets 15.1 amd64 with Python 3.12. This experimental target validates
installation and usage on one architecture; ARM64 is not part of the CI scope.

Install the system interpreter, its SQLite module, and the toolchain needed to
build Python dependencies that have no FreeBSD wheels:

```sh
pkg install git uv python312 py312-sqlite3 rust pkgconf
```

Then, as your regular user, use the standard isolated installation:

```sh
uv tool install --python /usr/local/bin/python3.12 git+https://github.com/nico97118/git-ez
export PATH="$(uv tool dir --bin):$PATH"
export MANPATH="$(uv tool dir)/git-ez-ai/share/man:${MANPATH:-}"
git ez --version
git help ez
```

For a local checkout, replace the Git URL with `.`. Save the exports in your
shell profile (these examples use POSIX shell syntax). No system Python packages
need to be exposed to the tool environment. Neither `sqlite-vec` nor a SQLite
checkpoint adapter is required. Graph checkpoints stay in memory during each
agent session; durable audit events and agent files are stored separately.

For development prerequisites and the FreeBSD-specific Ruff warning, see
[Contributing on FreeBSD](../CONTRIBUTING.md#freebsd). Ruff is not required at runtime.

Dependencies without matching FreeBSD wheels compile from source. The FreeBSD CI
runs the real conflict-resolution tests and verifies an installed wheel in an
isolated environment for each release target.
