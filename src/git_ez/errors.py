# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Errors safe to present at the CLI boundary."""


class EzError(Exception):
    """An expected configuration, Git, model, or resolution failure."""
