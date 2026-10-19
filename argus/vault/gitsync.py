"""Git sync for the vault: pull --rebase → write → commit as Argus → push. Never force-push.

On a rebase conflict, the rebase is aborted and the branch is reset to the remote: the
student's version wins, and Argus re-derives its writes on the next cycle, where the
ownership rules turn any disagreement into a proposal. Three consecutive failed cycles raise
a system alert (via `vault_runs`).
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

AUTHOR_NAME = "Argus"
AUTHOR_EMAIL = "argus@pi.local"
GIT_TIMEOUT_S = 120


class GitError(Exception):
    pass


@dataclass(frozen=True)
class SyncResult:
    status: str  # ok | conflict | error | skipped
    commit: str | None = None
    files: int = 0
    error: str | None = None


class GitSync:
    def __init__(self, root: Path, *, branch: str | None = None) -> None:
        self.root = root
        self._branch = branch

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "GIT_AUTHOR_NAME": AUTHOR_NAME,
                "GIT_AUTHOR_EMAIL": AUTHOR_EMAIL,
                "GIT_COMMITTER_NAME": AUTHOR_NAME,
                "GIT_COMMITTER_EMAIL": AUTHOR_EMAIL,
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        return env

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(  # noqa: S603 (fixed git binary, no shell)
            ["git", *args],  # noqa: S607
            cwd=self.root,
            env=self._env(),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
        if check and result.returncode != 0:
            raise GitError(f"git {args[0]} failed: {result.stderr.strip()[:300]}")
        return result

    def ensure_repo(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if not (self.root / ".git").exists():
            self.git("init", "-q", "-b", "main")
            self.git("commit", "-q", "--allow-empty", "-m", "Argus: start vault")

    @property
    def branch(self) -> str:
        return self._branch or self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    def has_remote(self) -> bool:
        return bool(self.git("remote").stdout.strip())

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").stdout.strip()

    def _rebasing(self) -> bool:
        git_dir = self.root / ".git"
        return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()

    def pull(self) -> str:
        """'ok', or 'conflict' after keeping the remote (student's) version."""
        if not self.has_remote():
            return "ok"
        self.git("fetch", "-q", "origin")
        remote = f"origin/{self.branch}"
        if self.git("rev-parse", "--verify", "-q", remote, check=False).returncode != 0:
            return "ok"  # empty remote: our first push creates the branch
        result = self.git("rebase", remote, check=False)
        if result.returncode == 0:
            return "ok"
        if self._rebasing():
            self.git("rebase", "--abort", check=False)
        self.git("reset", "-q", "--hard", remote)  # the student's version wins
        return "conflict"

    def commit(self, message: str) -> int:
        self.git("add", "-A")
        changed = [line for line in self.git("status", "--porcelain").stdout.splitlines() if line]
        if not changed:
            return 0
        self.git("commit", "-q", "-m", message)
        return len(changed)

    def push(self) -> None:
        if self.has_remote():
            self.git("push", "-q", "origin", f"HEAD:{self.branch}")  # never --force

    def cycle(self, write: Callable[[], str]) -> SyncResult:
        """One sync: pull, write (returns a commit message), commit, push."""
        try:
            self.ensure_repo()
            if self.git("status", "--porcelain").stdout.strip():
                # Leftovers from an interrupted cycle: commit them before pulling.
                self.commit("Argus: finish interrupted update")
            if self.pull() == "conflict":
                return SyncResult(
                    "conflict", error="your changes conflicted with Argus's; kept yours"
                )
            message = write()
            files = self.commit(message)
            self.push()
            return SyncResult("ok", commit=self.head(), files=files)
        except (GitError, subprocess.TimeoutExpired, OSError) as e:
            return SyncResult("error", error=str(e)[:500])
