"""Checks owned by Git orchestration, independent of the external agent harness."""

from .audit import Audit
from .config import Config
from .conflicts import MARKERS
from .errors import EzError
from .git import Git


class GitStateChanged(EzError):
    """An agent or concurrent actor changed state owned by the orchestrator."""


class WorktreeChecks:
    def __init__(self, config: Config):
        self.config = config

    def guard(self, git: Git, audit: Audit, head: str, branch: str | None) -> None:
        changed = git.text("rev-parse", "HEAD") != head
        if branch is not None:
            changed |= git.text("symbolic-ref", "-q", "HEAD") != branch
        else:
            changed |= bool(git.run("symbolic-ref", "-q", "HEAD", check=False).stdout)
        changed |= git.branch_oid(audit.manifest["branch"]) != audit.manifest["before_oid"]
        ref = audit.manifest["target_ref"]
        if ref:
            changed |= git.commit_oid(ref) != audit.manifest["target_oid"]
        if audit.manifest["operation"] == "rebase":
            changed |= not git.rebase_active()
        else:
            merge_head = git.run("rev-parse", "--verify", "MERGE_HEAD", check=False)
            changed |= merge_head.returncode != 0 or (
                merge_head.stdout.decode().strip() != audit.manifest["target_oid"]
            )
        if changed:
            raise GitStateChanged(
                "Agent changed Git control state or input refs; integration stopped"
            )

    def verify(self, git: Git, conflicts: list[str]) -> None:
        raw = git.run("diff", "--name-only", "-z", "HEAD", "--").stdout
        raw += git.run("ls-files", "--others", "--exclude-standard", "-z").stdout
        names = set(raw.decode("utf-8").split("\0")) | set(conflicts)
        for name in names - {""}:
            path = git.safe_path(name)
            if not path.exists():
                continue
            if not path.is_file() or path.stat().st_size > self.config.max_file_bytes:
                raise EzError(f"Changed file is not regular text or exceeds size limit: {name}")
            text = path.read_bytes().decode("utf-8")
            if "\0" in text or MARKERS.search(text):
                raise EzError(f"Binary content or conflict markers remain in {name}")
