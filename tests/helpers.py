# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Small helpers shared by integration scenarios."""

from git_ez.engine import Engine


def integrate(repo, operation="rebase", target="main", **kwargs):
    return Engine(repo.root, repo.config, repo.resolver).run(target, operation, **kwargs)
