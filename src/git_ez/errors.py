# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Errors safe to present at the CLI boundary."""


class EzError(Exception):
    """An expected configuration, Git, model, or resolution failure."""


class AuditError(EzError):
    """An unmet audit obligation, distinct from an execution/provider failure."""

    def __init__(self, message: str, artifact: str | None = None):
        super().__init__(message)
        self.artifact = artifact
