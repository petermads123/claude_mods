"""Smoke tests for the vendored PreToolUse hook, `.claude/hooks/guard_git.py`.

The guard is copied from template_repo, whose suite covers its parser in depth.
These only prove the copy works here: that it loads without template_repo's
`plan_state`, reads the branch by itself, and still refuses the plain slips
while allowing the plain work.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock

HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "guard_git.py"

#: Who the temporary repositories' commits are by, so no git config is needed.
IDENTITY = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def load_guard() -> ModuleType:
    """Load the guard as a module, by its file path."""
    spec = importlib.util.spec_from_file_location("guard_git_under_test", HOOK)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {HOOK}")
    module = importlib.util.module_from_spec(spec)
    # Registered first: a dataclass looks its own module up while it is built.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = load_guard()


def git_environment(**extra: str) -> dict[str, str]:
    """This process's environment, minus anything that aims git elsewhere."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    return {**env, **IDENTITY, **extra}


def git(repo: Path, *args: str) -> None:
    """Run git in `repo` with a fixed identity and no signing."""
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", *args],
        cwd=str(repo),
        env=git_environment(),
        capture_output=True,
        check=True,
        timeout=60,
    )


class ViolationTest(unittest.TestCase):
    """The plain cases `violation` decides."""

    def test_refuses_a_commit_on_main(self) -> None:
        self.assertIn("main", guard.violation("git commit -m x", "main"))

    def test_allows_a_commit_on_a_feature_branch(self) -> None:
        self.assertEqual(guard.violation("git commit -m x", "feat/x"), "")

    def test_allows_a_commit_after_switching_off_main(self) -> None:
        command = "git switch -c feat/x && git commit -m x"

        self.assertEqual(guard.violation(command, "main"), "")

    def test_refuses_a_push_to_main_from_a_feature_branch(self) -> None:
        self.assertIn("push", guard.violation("git push origin main", "feat/x"))

    def test_allows_a_command_that_only_mentions_git(self) -> None:
        self.assertEqual(guard.violation("echo git commit", "main"), "")


class CurrentBranchTest(unittest.TestCase):
    """The `current_branch` that replaces template_repo's `plan_state` import."""

    def setUp(self) -> None:
        """Create an empty folder to work in."""
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name).resolve()

    def test_names_the_branch_of_a_repository(self) -> None:
        git(self.folder, "init")
        git(self.folder, "commit", "--allow-empty", "-m", "seed")
        git(self.folder, "switch", "-c", "feat/smoke")

        with mock.patch.dict(os.environ, git_environment(), clear=True):
            branch = guard.current_branch(self.folder)

        self.assertEqual(branch, "feat/smoke")

    def test_is_empty_outside_a_repository(self) -> None:
        # The ceiling keeps git from finding a repository above the folder.
        ceiling = git_environment(GIT_CEILING_DIRECTORIES=str(self.folder.parent))

        with mock.patch.dict(os.environ, ceiling, clear=True):
            branch = guard.current_branch(self.folder)

        self.assertEqual(branch, "")


if __name__ == "__main__":
    unittest.main()
