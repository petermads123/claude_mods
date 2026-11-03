"""Stop hook: keep the turn going while the marketplace reports an error.

A machine that installed a mod from GitHub only takes a change that comes with
a higher version, so a change that leaves the version alone never reaches it,
and a marketplace entry that disagrees with the mod it lists breaks the install
itself. CI reports both on the pull request, but by then the turn that made the
slip is long over. This hook asks at the end of every turn instead, while the
change is still in Claude's hands.

It runs the quick half of the checker, `scripts/check_mods.py --changed
--no-cli`: the marketplace-wide checks and the mods the branch touched, without
`claude plugin validate` or the mods' tests, which are too slow for every stop.
It checks the checkout Claude works in. In a worktree session that is the
worktree: the payload's `cwd` follows Claude there, while `CLAUDE_PROJECT_DIR`
stays at the main checkout.

The checker's exit code decides:

- 0: the turn ends, silently, whatever warnings were printed.
- 1 with `error:` lines: the turn is blocked, and those lines are the reason.
  The reason also says that a fix which needs the user, such as a bump nobody
  agreed, is put to them instead, since the next stop is not blocked. In three
  cases the turn is not blocked at all; the lines go to the user as a note:
  - `main` or a detached HEAD is checked out. Nothing is committed there, and
    `/mod` moves the change to a branch, where the gate applies.
  - The turn was blocked once already (`stop_hook_active`). Blocking again
    could loop, and what still fails is the user's to hear about.
  - The only error is that git cannot read the repository: no `.git`, no git,
    or git refusing it. Nothing in the marketplace fixes that, and `git init`
    is not a fix.
- Anything else — another code, a run past the timeout, a checker that could
  not be started, or a 1 without an `error:` line, which is how a crash ends —
  means the gate failed, not the marketplace. A note goes to the user and the
  turn ends: a gate that blocks on its own failure holds the session hostage.

The gate also stands aside when `.claude/.skip-gate` exists, the deliberate
bypass, and when the checker is not there at all, as in a clone from before it
existed.

Stdlib only: `jq` may not be available and hook commands default to
Git Bash on Windows, so the usual shell recipe does not work here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

#: The checker, relative to the repository root.
CHECKER = Path("scripts") / "check_mods.py"

#: Its presence bypasses the gate. `.gitignore` keeps it to one machine.
SKIP_FILE = Path(".claude") / ".skip-gate"

#: The quick half: only the mods changed on the branch, and no `claude` CLI.
CHECKER_ARGS = ("--changed", "--no-cli")

#: Seconds the checker gets. Below the 150 s `settings.json` gives this hook,
#: so a checker that hangs is reported here instead of killed by Claude Code.
TIMEOUT_SECONDS = 120

#: Seconds each question to git gets: which checkout, and which branch.
GIT_TIMEOUT_SECONDS = 10

#: The checker's exit code for "at least one error"; 0 means none.
ERRORS_FOUND = 1

#: How the checker begins a line that reports an error.
ERROR_PREFIX = "error:"

#: How the checker begins its line for a folder git cannot read.
NO_REPOSITORY = "error: .: not a git repository"

#: The branch nothing is committed on: `guard_git.py` refuses a commit there.
PROTECTED = "main"

#: Opens the reason Claude is handed when the turn is blocked.
BLOCK_HEADER = (
    "The marketplace is not ready to hand back. Fix these, then stop again. If a "
    "fix needs the user — a version bump they have not agreed, or a check that "
    "has already failed twice — tell them that instead; the next stop is not "
    "blocked:\n\n"
)

#: Opens the note for errors on a checkout where nothing is committed.
NOT_COMMITTED = (
    "not blocking: nothing on {where} can be committed; /mod moves the change to "
    "a branch and raises the version"
)

#: How many lines of the checker's output a note about its failure quotes.
TAIL_LINES = 20


def parse_payload(data: bytes) -> dict[str, object]:
    """Decode the JSON object Claude Code writes to the hook's stdin.

    The bytes are decoded here rather than by `sys.stdin`, whose encoding is the
    locale's: on Windows that reads a UTF-8 byte-order mark as three letters and
    garbles a path that is not ASCII.

    Args:
        data: Everything read from stdin.

    Returns:
        The payload, or an empty one when stdin was empty.

    Raises:
        ValueError: The input is not JSON.
        TypeError: The input is JSON, but not an object.
    """
    text = data.decode("utf-8", errors="replace").lstrip("\ufeff").strip()
    payload = json.loads(text or "{}")
    if not isinstance(payload, dict):
        raise TypeError(f"expected a JSON object, got {type(payload).__name__}")
    return payload


def _git(directory: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    """Ask git about a directory.

    Args:
        directory: Where git runs, as `git -C` takes it.
        *args: The git command.

    Returns:
        The finished run, whatever its exit code; None when git could not be
        started or ran past `GIT_TIMEOUT_SECONDS`, which says nothing either way.
    """
    try:
        return subprocess.run(
            ["git", "-C", str(directory), *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):  # ValueError: a NUL
        return None


def project_root(payload: dict[str, object]) -> Path:
    """Find the checkout the turn worked in.

    The payload's `cwd` is where Claude works, a worktree included, while
    `CLAUDE_PROJECT_DIR` stays at the main checkout: in a worktree session it
    names a tree the session did not change and may not edit. So the checkout
    holding `cwd` comes first, at its top level, which also covers a shell that
    moved into a mod's folder.

    Args:
        payload: The decoded hook payload.

    Returns:
        The top level of the git checkout holding the payload's `cwd`, when it
        has the checker; else `CLAUDE_PROJECT_DIR` when it is set; else the
        hook's own working directory. Absolute.
    """
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        done = _git(Path(cwd), "rev-parse", "--show-toplevel")
        top = done.stdout.strip() if done is not None and done.returncode == 0 else ""
        if top and (Path(top) / CHECKER).is_file():
            return Path(top)
    configured = os.environ.get("CLAUDE_PROJECT_DIR")
    if configured:
        return Path(configured).absolute()
    return Path.cwd()


def current_branch(root: Path) -> str | None:
    """Name the branch a checkout has checked out.

    Args:
        root: The checkout.

    Returns:
        The branch's name; "" for a detached HEAD; None when git cannot say,
        as outside a repository.
    """
    done = _git(root, "branch", "--show-current")
    if done is None or done.returncode != 0:
        return None
    return done.stdout.strip()


def run_checker(root: Path) -> subprocess.CompletedProcess[str]:
    """Run the checker's quick half on the repository.

    Args:
        root: Repository root.

    Returns:
        The finished run, its output decoded as UTF-8.

    Raises:
        subprocess.TimeoutExpired: The checker ran past `TIMEOUT_SECONDS`.
        OSError: The interpreter could not be started.
    """
    command = [sys.executable, str(root / CHECKER), *CHECKER_ARGS, "--root", str(root)]
    return subprocess.run(
        command,
        cwd=str(root),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT_SECONDS,
        check=False,
        # The output is read as UTF-8, so the checker has to write it that way
        # even where the console's code page is something else.
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


def error_lines(output: str) -> list[str]:
    """Pick the checker's errors out of everything it printed.

    Args:
        output: What the checker wrote to one of its streams.

    Returns:
        The lines that begin with `error:`, in order, without trailing space.
    """
    return [
        line.rstrip() for line in output.splitlines() if line.startswith(ERROR_PREFIX)
    ]


def bullets(lines: list[str]) -> str:
    """List lines as a reason or a note shows them: one bullet each."""
    return "\n".join(f"- {line}" for line in lines)


def block_reason(errors: list[str]) -> str:
    """Word the reason a blocked turn hands Claude.

    Args:
        errors: The checker's `error:` lines.

    Returns:
        `BLOCK_HEADER`, then one bullet per error line.
    """
    return BLOCK_HEADER + bullets(errors)


def block(reason: str) -> NoReturn:
    """Tell Claude Code to keep going instead of stopping.

    Args:
        reason: Why the turn may not end yet.
    """
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


def note(message: str) -> NoReturn:
    """Show the user a message and let the turn end.

    Claude Code does not show the stderr of a hook that exits 0, so a gate that
    could not run would pass in silence; a `systemMessage` puts it in front of
    the user instead. stderr keeps a copy for the debug log.

    Args:
        message: What the user should know.
    """
    text = f"mod_gate.py: {message}"
    print(json.dumps({"systemMessage": text}))
    print(text, file=sys.stderr)
    sys.exit(0)


def judge(root: Path, errors: list[str], blocked_once: bool) -> NoReturn:
    """Block the turn on the checker's errors, or hand them to the user.

    Args:
        root: The checkout that was checked.
        errors: The checker's `error:` lines; at least one.
        blocked_once: Whether this turn was blocked once already.
    """
    # Claude cannot fix a repository git cannot read, so that line is never a
    # reason to keep going, though the user hears of it in any note.
    fixable = [line for line in errors if not line.startswith(NO_REPOSITORY)]
    if not fixable:
        note(
            "not blocking: git could not read the repository, so versions were "
            f"not compared:\n{bullets(errors)}"
        )
    branch = current_branch(root)
    if branch in (PROTECTED, ""):
        where = branch or "a detached HEAD"
        note(f"{NOT_COMMITTED.format(where=where)}:\n{bullets(errors)}")
    if blocked_once:
        note(f"still failing after one block:\n{bullets(errors)}")
    block(block_reason(fixable))


def _tail(*outputs: str) -> str:
    """Quote the last lines the checker printed, for a note."""
    lines = [line for text in outputs for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-TAIL_LINES:]) or "(no output)"


def main() -> None:
    """Decide whether the turn may end."""
    try:
        payload = parse_payload(sys.stdin.buffer.read())
    except (ValueError, TypeError) as exc:
        note(f"could not parse its payload, so nothing was checked: {exc}")

    root = project_root(payload)
    if (root / SKIP_FILE).exists():
        sys.exit(0)
    if not (root / CHECKER).is_file():
        sys.exit(0)  # nothing to run

    checker = CHECKER.as_posix()
    try:
        result = run_checker(root)
    except subprocess.TimeoutExpired:
        note(f"{checker} ran past {TIMEOUT_SECONDS} s, so nothing was checked.")
    except OSError as exc:
        note(f"{checker} could not be started, so nothing was checked: {exc}")

    if result.returncode == 0:
        sys.exit(0)

    errors = error_lines(result.stdout) + error_lines(result.stderr)
    if result.returncode == ERRORS_FOUND and errors:
        judge(root, errors, bool(payload.get("stop_hook_active")))

    if result.returncode == ERRORS_FOUND:
        what = "but printed no `error:` line, which is how a crash ends"
    else:
        what = "which is neither a pass (0) nor a list of errors (1)"
    note(
        f"{checker} exited with {result.returncode}, {what}; nothing was checked. "
        f"Its output ended:\n{_tail(result.stdout, result.stderr)}"
    )


if __name__ == "__main__":
    main()
