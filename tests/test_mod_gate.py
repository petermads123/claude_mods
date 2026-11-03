"""Tests for the Stop hook, `.claude/hooks/mod_gate.py`.

The hook runs the way Claude Code runs it: as a child of this interpreter, with
a JSON payload on stdin and `CLAUDE_PROJECT_DIR` naming a temporary root. That
root holds a stand-in `scripts/check_mods.py` whose exit code and output each
test chooses, and which records how it was called, so the hook is held to the
checker's contract rather than to whatever the real checker prints today.

Which checkout the hook checks, and on which branch, are git's answers, so
those tests build real git checkouts, a worktree among them. The rest run in
folders git does not know, where the hook can tell no branch and blocks as it
would on a work branch.

The two failures a child process cannot stage on demand, a checker that hangs
and an interpreter that cannot start, are driven in process instead, with the
module loaded by its file path.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock

HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "mod_gate.py"

HEADER = (
    "The marketplace is not ready to hand back. Fix these, then stop again. If a "
    "fix needs the user — a version bump they have not agreed, or a check that "
    "has already failed twice — tell them that instead; the next stop is not "
    "blocked:\n\n"
)

#: Stands in for scripts/check_mods.py. It records its arguments and working
#: directory in call.json beside itself, then does what plan.json says.
FAKE_CHECKER = """\
import json
import os
import sys
import time
from pathlib import Path

here = Path(__file__).resolve().parent
plan = json.loads((here / "plan.json").read_text(encoding="utf-8"))
call = {"argv": sys.argv[1:], "cwd": os.getcwd()}
(here / "call.json").write_text(json.dumps(call), encoding="utf-8")
time.sleep(plan["sleep"])
sys.stdout.write(plan["stdout"])
sys.stderr.write(plan["stderr"])
sys.exit(plan["code"])
"""

#: The checker's output when it finds problems: errors among everything else.
FAILING_OUTPUT = (
    "warning: plugins/usage-band/CHANGELOG.md: the first section is not 0.2.0\n"
    "error: plugins/usage-band: changed since main but version is still 0.1.0\n"
    "error: README.md: does not mention new-mod@claude-mods\n"
    "ok: other-mod 1.2.0 (main: 1.1.0)\n"
    "2 error(s), 1 warning(s)\n"
)
FAILING_ERRORS = [
    "error: plugins/usage-band: changed since main but version is still 0.1.0",
    "error: README.md: does not mention new-mod@claude-mods",
]

#: The checker's output when it finds nothing wrong, warnings and all.
PASSING_OUTPUT = (
    "warning: plugins/usage-band: hooks/hooks.json missing\n"
    "ok: usage-band 0.2.0 (main: 0.1.0)\n"
    "0 error(s), 1 warning(s)\n"
)

#: The checker's line for a folder git cannot read.
NO_REPOSITORY = (
    "error: .: not a git repository, or git is not installed: the version, "
    "--changed and generated-types checks did not run"
)

#: What the note on main or a detached HEAD opens with, before the place.
NOT_COMMITTED = "not blocking: nothing on {} can be committed; /mod moves"

#: Git's environment for the tests' own commands, and for the hook's.
GIT_ENV = {
    key: value
    for key, value in os.environ.items()
    if key not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "CLAUDE_PROJECT_DIR"}
}


def git(cwd: Path, *args: str) -> str:
    """Run git for a test, failing the test when git fails."""
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Mod Gate Test",
            "-c",
            "user.email=mod-gate@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=str(cwd),
        env=GIT_ENV,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed:\n{result.stderr}")
    return result.stdout.strip()


def make_checkout(path: Path, branch: str = "feat/work") -> Path:
    """Make `path` a git checkout with `branch` checked out, not yet committed."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", branch)
    return path


def install_checker(
    root: Path, code: int = 0, stdout: str = "", stderr: str = "", sleep: float = 0
) -> None:
    """Put a stand-in checker under `root` that behaves as told."""
    scripts = root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "check_mods.py").write_text(FAKE_CHECKER, encoding="utf-8")
    plan = {"code": code, "stdout": stdout, "stderr": stderr, "sleep": sleep}
    (scripts / "plan.json").write_text(json.dumps(plan), encoding="utf-8")


def checker_call(root: Path) -> dict[str, object] | None:
    """Read how the stand-in checker under `root` was called, if it was."""
    path = root / "scripts" / "call.json"
    if not path.exists():
        return None
    call: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    return call


def stop_payload(cwd: Path | None, active: bool = False) -> bytes:
    """Encode a Stop payload shaped like the one Claude Code sends."""
    payload: dict[str, object] = {
        "session_id": "test",
        "transcript_path": "",
        "hook_event_name": "Stop",
        "stop_hook_active": active,
    }
    if cwd is not None:
        payload["cwd"] = str(cwd)
    return json.dumps(payload).encode("utf-8")


def load_hook() -> ModuleType:
    """Load the hook as a module, by its file path."""
    spec = importlib.util.spec_from_file_location("mod_gate_under_test", HOOK)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {HOOK}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class GateTestCase(unittest.TestCase):
    """A temporary project root, and the hook run against it as a child."""

    def setUp(self) -> None:
        """Create the root."""
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def subroot(self, name: str) -> Path:
        """Create a second project root inside the first."""
        path = self.root / name
        path.mkdir()
        return path

    def run_hook(
        self, stdin: bytes, project_dir: Path | None, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run the hook as Claude Code would, `CLAUDE_PROJECT_DIR` as given.

        Git looks for a repository no higher than the temporary folder, so a
        folder a test did not make a checkout is outside git wherever the
        suite runs.
        """
        env = dict(GIT_ENV, GIT_CEILING_DIRECTORIES=str(self.root.parent))
        if project_dir is not None:
            env["CLAUDE_PROJECT_DIR"] = str(project_dir)
        done = subprocess.run(
            [sys.executable, str(HOOK)],
            input=stdin,
            capture_output=True,
            cwd=str(cwd or self.root),
            env=env,
            timeout=60,
            check=False,
        )
        return subprocess.CompletedProcess(
            done.args,
            done.returncode,
            done.stdout.decode("utf-8", errors="replace"),
            done.stderr.decode("utf-8", errors="replace"),
        )

    def assert_silent(self, result: subprocess.CompletedProcess[str]) -> None:
        """The turn may end, and the hook said nothing."""
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def assert_blocks(
        self, result: subprocess.CompletedProcess[str], errors: list[str]
    ) -> None:
        """The turn is blocked, with exactly these error lines as the reason."""
        self.assertEqual(result.returncode, 0, result.stderr)
        reason = HEADER + "\n".join(f"- {line}" for line in errors)
        self.assertEqual(
            json.loads(result.stdout), {"decision": "block", "reason": reason}
        )

    def assert_notes(
        self, result: subprocess.CompletedProcess[str], *fragments: str
    ) -> None:
        """The turn may end, with a note for the user carrying every fragment.

        The note is a `systemMessage` on stdout, which the user sees, with a
        copy on stderr for the debug log. It never blocks.
        """
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertNotIn("decision", output)
        message = output["systemMessage"]
        copy = result.stderr.replace("\r\n", "\n")  # stderr is text mode on Windows
        for fragment in fragments:
            self.assertIn(fragment, message)
            self.assertIn(fragment, copy)

    def assert_checked(self, root: Path) -> None:
        """The stand-in checker under `root` ran, on that root."""
        call = checker_call(root)
        if call is None:
            self.fail(f"the checker under {root} was not run")
        argv = call["argv"]
        assert isinstance(argv, list)
        self.assertEqual(["--changed", "--no-cli", "--root"], argv[:3])
        self.assertTrue(os.path.samefile(argv[3], root), argv[3])
        self.assertTrue(os.path.samefile(str(call["cwd"]), root))


class PayloadTest(GateTestCase):
    """How the hook reads what Claude Code sends it."""

    def test_a_turn_blocked_once_already_ends_with_what_still_fails(self) -> None:
        install_checker(self.root, code=1, stdout=FAILING_OUTPUT)

        result = self.run_hook(stop_payload(self.root, active=True), self.root)

        self.assert_notes(
            result,
            "mod_gate.py: still failing after one block:\n",
            *(f"- {line}" for line in FAILING_ERRORS),
        )
        self.assert_checked(self.root)

    def test_a_turn_blocked_once_already_ends_silently_once_clean(self) -> None:
        install_checker(self.root, code=0, stdout=PASSING_OUTPUT)

        result = self.run_hook(stop_payload(self.root, active=True), self.root)

        self.assert_silent(result)
        self.assert_checked(self.root)

    def test_an_unparseable_payload_is_noted_and_the_turn_ends(self) -> None:
        install_checker(self.root, code=1, stdout=FAILING_OUTPUT)

        result = self.run_hook(b"{not json", self.root)

        self.assert_notes(result, "mod_gate.py", "could not parse its payload")
        self.assertIsNone(checker_call(self.root))

    def test_a_payload_that_is_not_an_object_is_noted(self) -> None:
        install_checker(self.root, code=1, stdout=FAILING_OUTPUT)

        result = self.run_hook(b'["stop_hook_active"]', self.root)

        self.assert_notes(result, "expected a JSON object, got list")
        self.assertIsNone(checker_call(self.root))

    def test_a_payload_with_a_byte_order_mark_is_read(self) -> None:
        install_checker(self.root, code=1, stdout=FAILING_OUTPUT)

        result = self.run_hook(b"\xef\xbb\xbf" + stop_payload(self.root), self.root)

        self.assert_blocks(result, FAILING_ERRORS)

    def test_a_byte_order_mark_does_not_hide_stop_hook_active(self) -> None:
        install_checker(self.root, code=1, stdout=FAILING_OUTPUT)
        payload = b"\xef\xbb\xbf" + stop_payload(self.root, active=True)

        result = self.run_hook(payload, self.root)

        self.assert_notes(result, "still failing after one block")

    def test_an_empty_stdin_is_an_empty_payload(self) -> None:
        install_checker(self.root, code=1, stdout=FAILING_OUTPUT)

        result = self.run_hook(b"", self.root)

        self.assert_blocks(result, FAILING_ERRORS)


class RootTest(GateTestCase):
    """Which checkout the hook checks.

    In a worktree session `CLAUDE_PROJECT_DIR` names the main checkout and the
    payload's `cwd` the worktree, so `cwd` decides whenever it is in a git
    checkout that has the checker.
    """

    def test_the_worktree_claude_works_in_wins_over_the_main_checkout(self) -> None:
        main = make_checkout(self.root / "main-checkout")
        install_checker(main, code=1, stdout="error: main: the main checkout\n")
        git(main, "add", "-A")
        git(main, "commit", "-q", "-m", "the checker")
        worktree = self.root / "worktree"
        git(main, "worktree", "add", "-q", "-b", "feat/elsewhere", str(worktree))
        install_checker(worktree, code=1, stdout="error: wt: the worktree\n")

        result = self.run_hook(stop_payload(worktree), main, cwd=main)

        self.assert_blocks(result, ["error: wt: the worktree"])
        self.assert_checked(worktree)
        self.assertIsNone(checker_call(main))

    def test_a_cwd_in_a_mods_folder_is_raised_to_its_checkout(self) -> None:
        checkout = make_checkout(self.root / "checkout")
        install_checker(checkout, code=1, stdout="error: top: the checkout\n")
        mod = checkout / "plugins" / "usage-band"
        mod.mkdir(parents=True)
        elsewhere = self.subroot("elsewhere")
        install_checker(elsewhere, code=1, stdout="error: elsewhere: the env\n")

        result = self.run_hook(stop_payload(mod), elsewhere)

        self.assert_blocks(result, ["error: top: the checkout"])
        self.assert_checked(checkout)
        self.assertIsNone(checker_call(elsewhere))

    def test_a_cwd_outside_any_checkout_gives_way_to_claude_project_dir(self) -> None:
        project = make_checkout(self.root / "project")
        install_checker(project, code=1, stdout="error: project: the env\n")
        outside = self.subroot("outside")
        install_checker(outside, code=1, stdout="error: outside: from cwd\n")

        result = self.run_hook(stop_payload(outside), project)

        self.assert_blocks(result, ["error: project: the env"])
        self.assertIsNone(checker_call(outside))

    def test_a_checkout_without_the_checker_gives_way_too(self) -> None:
        other = make_checkout(self.root / "other-repo")
        project = make_checkout(self.root / "project")
        install_checker(project, code=1, stdout="error: project: the env\n")

        result = self.run_hook(stop_payload(other), project)

        self.assert_blocks(result, ["error: project: the env"])

    def test_without_git_or_claude_project_dir_the_working_directory_is_used(
        self,
    ) -> None:
        payload_dir, process_dir = self.subroot("payload"), self.subroot("process")
        install_checker(payload_dir, code=1, stdout="error: payload: from cwd\n")
        install_checker(process_dir, code=1, stdout="error: process: from getcwd\n")

        result = self.run_hook(stop_payload(payload_dir), None, cwd=process_dir)

        self.assert_blocks(result, ["error: process: from getcwd"])
        self.assertIsNone(checker_call(payload_dir))

    def test_the_working_directory_is_used_without_either(self) -> None:
        install_checker(self.root, code=1, stdout="error: here: from getcwd\n")

        result = self.run_hook(stop_payload(None), None, cwd=self.root)

        self.assert_blocks(result, ["error: here: from getcwd"])


class BranchTest(GateTestCase):
    """Nothing is committed on main or a detached HEAD, so nothing there blocks."""

    def checkout(self, branch: str, code: int = 1, stdout: str = "") -> Path:
        """A checkout on `branch` whose checker answers as told."""
        root = make_checkout(self.root / "checkout", branch)
        install_checker(root, code=code, stdout=stdout or FAILING_OUTPUT)
        return root

    def test_errors_on_main_are_shown_not_blocked(self) -> None:
        root = self.checkout("main")

        result = self.run_hook(stop_payload(root), root)

        self.assert_notes(
            result,
            NOT_COMMITTED.format("main"),
            "a branch and raises the version:\n",
            *(f"- {line}" for line in FAILING_ERRORS),
        )

    def test_errors_on_a_detached_head_are_shown_not_blocked(self) -> None:
        root = self.checkout("feat/work")
        git(root, "commit", "-q", "--allow-empty", "-m", "a commit to detach at")
        git(root, "checkout", "-q", "--detach")

        result = self.run_hook(stop_payload(root), root)

        self.assert_notes(result, NOT_COMMITTED.format("a detached HEAD"))

    def test_errors_on_a_work_branch_block(self) -> None:
        for branch in ("feat/work", "chore/maintain", "mainline"):
            with self.subTest(branch=branch):
                root = make_checkout(self.root / branch.replace("/", "-"), branch)
                install_checker(root, code=1, stdout=FAILING_OUTPUT)

                result = self.run_hook(stop_payload(root), root)

                self.assert_blocks(result, FAILING_ERRORS)

    def test_a_clean_run_on_main_is_silent(self) -> None:
        root = self.checkout("main", code=0, stdout=PASSING_OUTPUT)

        result = self.run_hook(stop_payload(root), root)

        self.assert_silent(result)


class NoRepositoryTest(GateTestCase):
    """A folder git cannot read is the user's to hear about, not Claude's to fix."""

    def test_only_a_repository_git_cannot_read_is_noted_not_blocked(self) -> None:
        output = f"{NO_REPOSITORY}\nok: usage-band 0.1.0 (main: not compared)\n"
        install_checker(self.root, code=1, stdout=output)

        result = self.run_hook(stop_payload(self.root), self.root)

        self.assert_notes(
            result,
            "not blocking: git could not read the repository",
            f"- {NO_REPOSITORY}",
        )

    def test_other_errors_still_block_without_that_line(self) -> None:
        output = f"{NO_REPOSITORY}\n{FAILING_OUTPUT}"
        install_checker(self.root, code=1, stdout=output)

        result = self.run_hook(stop_payload(self.root), self.root)

        self.assert_blocks(result, FAILING_ERRORS)


class StandAsideTest(GateTestCase):
    """When the hook lets the turn end without running the checker."""

    def test_the_skip_file_bypasses_the_gate(self) -> None:
        install_checker(self.root, code=1, stdout=FAILING_OUTPUT)
        (self.root / ".claude").mkdir()
        (self.root / ".claude" / ".skip-gate").touch()

        result = self.run_hook(stop_payload(self.root), self.root)

        self.assert_silent(result)
        self.assertIsNone(checker_call(self.root))

    def test_a_missing_checker_lets_the_turn_end_silently(self) -> None:
        result = self.run_hook(stop_payload(self.root), self.root)

        self.assert_silent(result)

    def test_a_folder_named_like_the_checker_is_not_run(self) -> None:
        (self.root / "scripts" / "check_mods.py").mkdir(parents=True)

        result = self.run_hook(stop_payload(self.root), self.root)

        self.assert_silent(result)


class VerdictTest(GateTestCase):
    """What the hook does with each answer the checker gives."""

    def test_the_checker_runs_on_the_changed_mods_without_the_cli(self) -> None:
        install_checker(self.root)

        self.run_hook(stop_payload(self.root), self.root)

        call = checker_call(self.root)
        if call is None:
            self.fail("the checker was not run")
        self.assertEqual(
            call["argv"], ["--changed", "--no-cli", "--root", str(self.root)]
        )
        self.assertTrue(os.path.samefile(str(call["cwd"]), self.root))

    def test_a_passing_checker_ends_the_turn_silently(self) -> None:
        install_checker(self.root, code=0, stdout=PASSING_OUTPUT)

        result = self.run_hook(stop_payload(self.root), self.root)

        self.assert_silent(result)

    def test_a_failing_checker_blocks_with_only_its_error_lines(self) -> None:
        install_checker(self.root, code=1, stdout=FAILING_OUTPUT)

        result = self.run_hook(stop_payload(self.root), self.root)

        self.assert_blocks(result, FAILING_ERRORS)

    def test_error_lines_are_taken_from_both_streams(self) -> None:
        install_checker(
            self.root,
            code=1,
            stdout="error: plugins/a: from stdout\n1 error(s), 0 warning(s)\n",
            stderr="error: plugins/b: from stderr\n",
        )

        result = self.run_hook(stop_payload(self.root), self.root)

        self.assert_blocks(
            result, ["error: plugins/a: from stdout", "error: plugins/b: from stderr"]
        )

    def test_an_error_line_that_is_not_ascii_arrives_intact(self) -> None:
        line = "error: plugins/über-band: version is still 0.1.0 — raise it → 0.1.1"
        install_checker(self.root, code=1, stdout=line + "\n")

        result = self.run_hook(stop_payload(self.root), self.root)

        self.assert_blocks(result, [line])

    def test_only_lines_that_start_with_error_count(self) -> None:
        install_checker(
            self.root,
            code=1,
            stdout="  error: indented, so a quote\nnote: error: not first\n"
            "error: plugins/a: the real one\n",
        )

        result = self.run_hook(stop_payload(self.root), self.root)

        self.assert_blocks(result, ["error: plugins/a: the real one"])

    def test_exit_one_without_an_error_line_is_noted_not_blocked(self) -> None:
        traceback = (
            "Traceback (most recent call last):\n"
            '  File "scripts/check_mods.py", line 1, in <module>\n'
            "KeyError: 'plugins'\n"
        )
        install_checker(self.root, code=1, stderr=traceback)

        result = self.run_hook(stop_payload(self.root), self.root)

        self.assert_notes(result, "exited with 1", "no `error:` line", "KeyError")

    def test_another_exit_code_is_noted_and_the_turn_ends(self) -> None:
        for code in (2, 3):
            with self.subTest(code=code):
                install_checker(
                    self.root,
                    code=code,
                    stdout="error: plugins/a: not a verdict this time\n",
                    stderr="usage: check_mods.py [--mod NAME]...\n",
                )

                result = self.run_hook(stop_payload(self.root), self.root)

                self.assert_notes(result, f"exited with {code}", "usage:")


class OwnFailureTest(unittest.TestCase):
    """Failures of the gate itself, staged in process."""

    hook: ModuleType

    @classmethod
    def setUpClass(cls) -> None:
        """Load the hook once for the class."""
        cls.hook = load_hook()

    def setUp(self) -> None:
        """Create a project root."""
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def run_main(self) -> tuple[object, str, str]:
        """Call the hook's `main` on a Stop payload for the root."""
        stdin = io.TextIOWrapper(io.BytesIO(stop_payload(self.root)), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(sys, "stdin", stdin),
            mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(self.root)}),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
            self.assertRaises(SystemExit) as caught,
        ):
            self.hook.main()
        return caught.exception.code, out.getvalue(), err.getvalue()

    def test_a_checker_that_runs_past_its_timeout_is_noted(self) -> None:
        install_checker(self.root, code=1, stdout=FAILING_OUTPUT, sleep=30)

        with mock.patch.object(self.hook, "TIMEOUT_SECONDS", 0.5):
            code, out, err = self.run_main()

        self.assertEqual(code, 0)
        self.assertIn("ran past 0.5 s", json.loads(out)["systemMessage"])
        self.assertIn("ran past 0.5 s", err)

    def test_an_interpreter_that_cannot_start_is_noted(self) -> None:
        install_checker(self.root, code=1, stdout=FAILING_OUTPUT)
        missing = str(self.root / "no-such-python")

        with mock.patch.object(sys, "executable", missing):
            code, out, err = self.run_main()

        self.assertEqual(code, 0)
        self.assertIn("could not be started", json.loads(out)["systemMessage"])
        self.assertIn("could not be started", err)
        self.assertIsNone(checker_call(self.root))

    def test_a_git_that_cannot_answer_leaves_the_root_to_the_environment(
        self,
    ) -> None:
        project = {"CLAUDE_PROJECT_DIR": str(self.root)}
        for failure in (OSError("no git"), subprocess.TimeoutExpired(["git"], 10)):
            with (
                self.subTest(failure=type(failure).__name__),
                mock.patch.object(self.hook.subprocess, "run", side_effect=failure),
                mock.patch.dict(os.environ, project),
            ):
                root = self.hook.project_root({"cwd": str(self.root / "plugins")})
                self.assertEqual(self.root.absolute(), root)
                self.assertIsNone(self.hook.current_branch(self.root))

    def test_a_cwd_git_cannot_take_leaves_the_root_to_the_environment(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(self.root)}):
            root = self.hook.project_root({"cwd": "a\0path"})
        self.assertEqual(self.root.absolute(), root)


if __name__ == "__main__":
    unittest.main()
