"""Errors safe to present at the CLI boundary."""


class EzError(Exception):
    """An expected configuration, Git, model, or resolution failure."""
