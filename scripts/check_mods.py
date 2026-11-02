"""Check the marketplace and its mods: the gate every change to this repo passes.

Machines install the mods in this repo from GitHub and take a change only when
it arrives with a higher version. The mistakes that matter are therefore quiet
ones: a mod changed without a bump never reaches anyone, a version written into
its marketplace entry is ignored, a folder no entry lists cannot be installed,
and none of them fails anything at the time it is made. So they are checked
mechanically, by this script: before every commit (`/verify`), when a turn ends
(the Stop hook, `--changed --no-cli`) and in CI (`--no-cli`).

How it decides:

- **Whole-repo checks** always run, whatever is selected: the marketplace file,
  the README's install lines, and generated types that git tracks.
- **Per-mod checks** run on the selected mods: every mod the marketplace lists
  or `plugins/` holds a folder for, narrowed by `--mod`, and by `--changed` to
  mods with any file changed against the base.
- **Versions** are compared against the base: the merge base of HEAD and
  `--base REF`, else `origin/main`, else `main`. A change counts whether it is
  committed, staged, unstaged or untracked, because the bump is due before the
  work is committed, not after. A changed file the mod ships needs the version
  raised; its changelog, README, tsconfig, tests and generated types do not ship.
- **Git's answers** are the versions' evidence, so a git command that fails or
  runs past its timeout is an error, never an empty answer: read as "nothing
  changed", it would pass the very change this script exists to stop. So is a
  base that shares no history with HEAD, as in a shallow clone.
- **The CLI**, `claude plugin validate` and `claude plugin test`, runs last
  unless `--no-cli`: it is the slow half, and the only half that knows what the
  installed engine accepts.

Output is one `error:` or `warning:` line per problem, then an `ok:` line per
selected mod without errors, then the counts. Exit 0 without errors (warnings
allowed), 1 with any, 2 for a usage error.

Stdlib only, for Python 3.10 and later: CI runs it on a bare runner and the
Stop hook on whichever Python the machine has.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

ERROR = "error"
WARNING = "warning"

MARKETPLACE = ".claude-plugin/marketplace.json"
PLUGINS = "plugins"
README = "README.md"
# Where a finding about the repo as a whole, rather than one file in it, points.
REPO = "."

# Arguments for every `claude plugin validate`. "--strict" turns the engine's
# warnings into failures: an entry version that disagrees with plugin.json, an
# unknown manifest key, a missing author or marketplace description. The checks
# below require the last two, so a repo that passes here passes strict too.
VALIDATE_FLAGS: tuple[str, ...] = ("--strict",)

CLI_TIMEOUT_SECONDS = 180
GIT_TIMEOUT_SECONDS = 60
# A failed command's errors come last in its output; this many lines hold them.
TAIL_LINES = 20

KEBAB = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
# A part of X.Y.Z: a whole number without leading zeros, as semver has it, so
# that `claude plugin tag` accepts the version after the merge. [0-9] rather
# than \d, which also matches the digits of other scripts.
NUMBER = r"(?:0|[1-9][0-9]*)"
X_Y_Z = rf"{NUMBER}\.{NUMBER}\.{NUMBER}"
VERSION = re.compile(rf"({NUMBER})\.({NUMBER})\.({NUMBER})")
# `## 1.2.3`, `## [1.2.3]` or `## v1.2.3`, then whitespace or the end of the line.
CHANGELOG_HEADING = re.compile(
    rf"^##[ \t]+(?:\[({X_Y_Z})\]|v?({X_Y_Z}))(?=\s|$)", re.MULTILINE
)
CLI_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)+(?:[-+][0-9A-Za-z.-]+)?")
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# Files a mod's folder holds that change nothing an installed copy does.
NOT_SHIPPED = frozenset({"CHANGELOG.md", "README.md", "tsconfig.json"})
TEST_SUFFIXES = (".test.ts", ".test.tsx")
# Written by Claude Code when it loads a mod with --plugin-dir, or writes one;
# never shared.
GENERATED = (".claude-plugin", "types")

BOM = "saved with a byte-order mark; save as UTF-8 without BOM"

# Plugin names Claude Code refuses: these exactly, anything with these prefixes,
# and "official" beside one of BRAND_WORDS. A brand word anywhere else in a name
# draws the engine's warning instead.
RESERVED_NAMES = frozenset(
    {"claude", "anthropic", "anthropics", "claude-code", "claude-mods"}
)
RESERVED_PREFIXES = ("claude-", "anthropic-", "anthropics-", "cc-plugin-")
BRAND_WORDS = frozenset({"claude", "anthropic", "anthropics"})

STRICT = "`claude plugin validate --strict` fails without it"


@dataclass(frozen=True)
class Problem:
    """One finding, printed as `<level>: <where>: <message>`.

    Attributes:
        level: `error`, which fails the run, or `warning`, which does not.
        where: Repo-relative POSIX path of what the finding is about; `.` for
            the repo as a whole.
        message: What is wrong and, when it is not obvious, how to put it right.
        mod: The mod a finding outside its folder is about, such as one in its
            marketplace entry or its README line, so that an error there
            withholds the mod's `ok:` line too; "" otherwise. Not printed, and
            not part of what makes two findings the same.
    """

    level: str
    where: str
    message: str
    mod: str = field(default="", compare=False)

    def __str__(self) -> str:
        """Render the finding as its output line."""
        return f"{self.level}: {self.where}: {self.message}"


@dataclass(frozen=True)
class Marketplace:
    """What the other checks need from `marketplace.json`.

    Attributes:
        name: The marketplace's name, the `<name>` in `<mod>@<name>`; empty when
            the file gives none.
        entries: Each listed mod's entry, by name. A name listed twice keeps its
            first entry.
    """

    name: str
    entries: dict[str, dict[str, object]] = field(default_factory=dict)


@dataclass(frozen=True)
class _Json:
    """A JSON file as read: whether it exists, its value, and what went wrong."""

    found: bool
    data: object = None
    error: str = ""
    bom: bool = False


@dataclass(frozen=True)
class _Report:
    """What one run found, ready to print."""

    problems: list[Problem]
    oks: list[str]
    cli_version: str | None


def _error(where: str, message: str, mod: str = "") -> Problem:
    return Problem(ERROR, where, message, mod)


def _warning(where: str, message: str, mod: str = "") -> Problem:
    return Problem(WARNING, where, message, mod)


def _show(value: object) -> str:
    """Quote a JSON value for a message, shortened when it is long."""
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 60 else text[:57] + "..."


def _read_json(path: Path) -> _Json:
    """Read a JSON file the way an editor on any platform may have saved it.

    Args:
        path: The file.

    Returns:
        What was found; `error` says why no value could be had.
    """
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return _Json(found=False)
    except OSError as exc:
        return _Json(found=True, error=f"cannot be read: {exc.strerror or exc}")
    # Windows PowerShell 5.1 writes UTF-16 unless told otherwise.
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return _Json(found=True, error="saved as UTF-16; save as UTF-8 without BOM")
    bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError:
        return _Json(found=True, error="not UTF-8 text", bom=bom)
    except json.JSONDecodeError as exc:
        at = f"line {exc.lineno}, column {exc.colno}"
        return _Json(found=True, error=f"not valid JSON: {exc.msg} ({at})", bom=bom)
    return _Json(found=True, data=data, bom=bom)


def _read_text(path: Path) -> str | None:
    """Read a text file, tolerating a BOM and stray bytes; None if unreadable."""
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None


def _inside(folder: Path, relative: str) -> bool:
    """Whether a path written relative to `folder` names a file inside it.

    An installed mod is a copy of its folder alone, so a path that climbs out
    of it works here and nowhere else.
    """
    try:
        target = (folder / relative).resolve()
        return target.is_file() and target.is_relative_to(folder.resolve())
    except (OSError, ValueError):  # ValueError: a NUL in the path
        return False


# --- versions ----------------------------------------------------------------


def parse_version(text: object) -> tuple[int, int, int] | None:
    """Read an `X.Y.Z` version.

    Args:
        text: The value of a `version` field, of whatever type it turned out
            to be.

    Returns:
        The three numbers, or None unless `text` is a string of exactly three
        dot-separated whole numbers without leading zeros: no prerelease, no
        build, no leading "v". `claude plugin tag` refuses "0.1.01" as semver
        does, and it runs only after the merge, when a fix costs another bump.
    """
    if not isinstance(text, str):
        return None
    match = VERSION.fullmatch(text)
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def _format(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def _next_versions(version: tuple[int, int, int]) -> list[tuple[int, int, int]]:
    """The patch, minor and major bump of a version, in that order."""
    major, minor, patch = version
    return [(major, minor, patch + 1), (major, minor + 1, 0), (major + 1, 0, 0)]


# --- git ---------------------------------------------------------------------


def _run(cmd: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run a command without input, capturing its output as UTF-8 text."""
    return subprocess.run(
        cmd,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


class GitError(Exception):
    """A git command the checks need failed, ran past its timeout or did not start.

    What changed, and what main held, are git's answers. A check that took a
    failure for an empty answer would see nothing changed and pass the very
    change it exists to stop, so the failure is raised instead, and the run
    reports it as an error.

    Attributes:
        command: The command, as `git <args>`.
        reason: Git's last line on stderr, or what else went wrong.
        timed_out: Whether git ran past `GIT_TIMEOUT_SECONDS`.
    """

    def __init__(self, args: list[str], reason: str, timed_out: bool = False) -> None:
        self.command = " ".join(["git", *args])
        self.reason = reason
        self.timed_out = timed_out
        super().__init__(f"{self.command} failed: {reason}")


def _git_run(root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run git in `root`, whatever its exit code.

    Raises:
        GitError: git ran past its timeout, or could not be started.
    """
    try:
        return _run(["git", *args], root, GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        reason = f"timed out after {GIT_TIMEOUT_SECONDS} s"
        raise GitError(args, reason, timed_out=True) from None
    except OSError as exc:
        raise GitError(args, f"could not start: {exc.strerror or exc}") from None


def _failure(args: list[str], result: subprocess.CompletedProcess[str]) -> GitError:
    """The error for a git command that exited with a failure."""
    lines = [line.strip() for line in (result.stderr or "").splitlines()]
    said = [line for line in lines if line]
    return GitError(args, said[-1] if said else f"exit {result.returncode}")


def _git(root: Path, args: list[str]) -> str:
    """Run a git command that has to succeed.

    Returns:
        Its output.

    Raises:
        GitError: It failed, ran past its timeout or could not be started.
    """
    result = _git_run(root, args)
    if result.returncode != 0:
        raise _failure(args, result)
    return result.stdout


def _git_probe(root: Path, args: list[str]) -> str | None:
    """Run a git command whose exit 1 answers "no".

    `rev-parse --verify --quiet` exits 1 for a name that resolves to nothing,
    a ref and a path absent from a commit alike, and `merge-base` for commits
    with no common ancestor. Any other failure is git's, not an answer.

    Returns:
        Its output, or None when it answered no.

    Raises:
        GitError: It failed any other way, ran past its timeout or could not
            be started.
    """
    result = _git_run(root, args)
    if result.returncode == 1:
        return None
    if result.returncode != 0:
        raise _failure(args, result)
    return result.stdout


def _resolve(root: Path, name: str) -> str | None:
    """The object a revision or a `<commit>:<path>` names, or None for none.

    Raises:
        GitError: git failed other than by finding nothing.
    """
    found = _git_probe(root, ["rev-parse", "--verify", "--quiet", name])
    return (found or "").strip() or None


def _git_paths(root: Path, args: list[str]) -> list[str]:
    """Run a git command that lists paths under `-z`.

    `-z` also turns off git's quoting, so a path with non-ASCII characters
    comes back as itself rather than as octal escapes in quotes.

    Raises:
        GitError: git failed.
    """
    return [path for path in _git(root, args).split("\0") if path]


def _is_git_repo(root: Path) -> bool:
    """Whether `root` is in a git work tree, with git installed to read it.

    Raises:
        GitError: git ran past its timeout, which answers neither way.
    """
    try:
        output = _git(root, ["rev-parse", "--is-inside-work-tree"])
    except GitError as exc:
        if exc.timed_out:
            raise
        return False  # not a repository, git refusing it, or no git at all
    return output.strip() == "true"


def _listing(root: Path, args: list[str]) -> list[str] | None:
    """Run a git listing that only a repository can answer.

    Returns:
        The paths, or None when `root` is not in a git work tree, or git is
        not installed: there is nothing for git to list.

    Raises:
        GitError: git failed in a repository.
    """
    try:
        return _git_paths(root, args)
    except GitError as exc:
        if exc.timed_out or _is_git_repo(root):
            raise
        return None


def _folders(root: Path) -> set[str]:
    """Every folder under `plugins/` but the dot-folders: what counts without git."""
    plugins = root / PLUGINS
    if not plugins.is_dir():
        return set()
    return {
        path.name
        for path in plugins.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    }


def _present_mods(root: Path) -> set[str]:
    """Name every folder under `plugins/` that holds a file git would ship.

    A folder holding only ignored files is not a mod: removing a mod with
    `git rm` leaves its generated types behind, and that leftover must not
    read as an unlisted mod. Without git, every folder counts.

    Args:
        root: Repository root.

    Returns:
        The folder names, dot-folders excluded.

    Raises:
        GitError: git could not list a repository's files.
    """
    listed = _listing(
        root,
        ["ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", PLUGINS],
    )
    if listed is None:
        return _folders(root)

    present: set[str] = set()
    for path in listed:
        parts = PurePosixPath(path).parts
        # --cached also lists files deleted from disk but not yet from the index.
        if (
            len(parts) >= 3
            and parts[0] == PLUGINS
            and not parts[1].startswith(".")
            and (root / path).is_file()
        ):
            present.add(parts[1])
    return present


def _mods_on_disk(root: Path) -> tuple[set[str], list[Problem]]:
    """The mods `_present_mods` finds, or every folder when git cannot say.

    Counting every folder errs towards checking too much, never too little,
    and the failure is reported.

    Returns:
        The folder names, and an error when git failed.
    """
    try:
        return _present_mods(root), []
    except GitError as exc:
        return _folders(root), [_error(REPO, str(exc))]


def _mods_at(root: Path, base: str) -> set[str]:
    """Name every mod that had a `plugin.json` at the base commit.

    Raises:
        GitError: git could not list the commit's files.
    """
    listed = _git_paths(
        root, ["ls-tree", "-r", "-z", "--name-only", base, "--", PLUGINS]
    )
    mods: set[str] = set()
    for path in listed:
        parts = PurePosixPath(path).parts
        if (
            len(parts) == 4
            and parts[0] == PLUGINS
            and not parts[1].startswith(".")
            and parts[2:] == (".claude-plugin", "plugin.json")
        ):
            mods.add(parts[1])
    return mods


def _json_at(root: Path, base: str, path: str) -> object:
    """A JSON file's value at the base commit.

    Args:
        root: Repository root.
        base: The base commit.
        path: The file, relative to `root`.

    Returns:
        The value, or None when the file was not there or is not JSON.

    Raises:
        GitError: git could not read the commit.
    """
    # `./` keeps the path relative to `root` even when it is not the top level.
    blob = _resolve(root, f"{base}:./{path}")
    if blob is None:
        return None  # not there at the base: an answer, not a failure
    text = _git(root, ["cat-file", "-p", blob])
    try:
        # A byte-order mark on main draws only a warning, so it must not hide
        # main's version from the comparison.
        return json.loads(text.lstrip("\ufeff"))
    except json.JSONDecodeError:
        return None


def _version_at(root: Path, base: str, mod: str) -> tuple[str | None, str]:
    """A mod's version at the base commit.

    Args:
        root: Repository root.
        base: The base commit.
        mod: A mod that had a `plugin.json` there.

    Returns:
        The version and "", or None and why it cannot be compared.

    Raises:
        GitError: git could not read the commit.
    """
    data = _json_at(root, base, f"{PLUGINS}/{mod}/.claude-plugin/plugin.json")
    if not isinstance(data, dict):
        return None, "main's plugin.json cannot be read as a JSON object"
    version = data.get("version")
    if not isinstance(version, str) or parse_version(version) is None:
        return None, f"main's version {_show(version)} is not X.Y.Z"
    return version, ""


def _current_version(root: Path, mod: str) -> str | None:
    """A mod's version in the working tree, when it is a valid X.Y.Z."""
    data = _read_json(root / PLUGINS / mod / ".claude-plugin" / "plugin.json").data
    version = data.get("version") if isinstance(data, dict) else None
    if isinstance(version, str) and parse_version(version) is not None:
        return version
    return None


def _name_at(root: Path, base: str) -> str:
    """The marketplace's name at the base commit; "" when it had none.

    Raises:
        GitError: git could not read the commit.
    """
    data = _json_at(root, base, MARKETPLACE)
    name = data.get("name") if isinstance(data, dict) else None
    return name if isinstance(name, str) else ""


def _marketplace_name(root: Path, base: str) -> str:
    """The marketplace's name at the base, else now: the name machines know.

    Raises:
        GitError: git could not read the commit.
    """
    name = _name_at(root, base)
    if not name:
        data = _read_json(root / MARKETPLACE).data
        now = data.get("name") if isinstance(data, dict) else None
        name = now if isinstance(now, str) else ""
    return name or "<marketplace>"


def _rename_problems(root: Path, base: str, name: str) -> list[Problem]:
    """Warn when the marketplace's name is not the one it had at the base.

    A machine knows a marketplace by the name it had when the machine added
    it, so after a rename `<mod>@<new name>` names nothing there.

    Args:
        root: Repository root.
        base: The commit to compare against.
        name: The marketplace's name now; "" when it has none.

    Returns:
        A warning when both names are there and differ; nothing otherwise.

    Raises:
        GitError: git could not read the commit.
    """
    old = _name_at(root, base)
    if not old or not name or old == name:
        return []
    message = (
        f"renamed from {old} to {name}: machines that added {old} keep it under "
        "that name, and the README's commands no longer match them"
    )
    return [_warning(MARKETPLACE, message)]


@dataclass(frozen=True)
class _Base:
    """The commit `_find_base` found, or why there is none.

    Attributes:
        commit: The merge base; None when there is none.
        ref: The candidate that resolved, with a merge base or without one;
            "" when none did.
    """

    commit: str | None
    ref: str = ""


def _find_base(root: Path, ref: str | None) -> _Base:
    """Find the commit to compare versions against, or why there is none.

    The first candidate that resolves decides. One that resolves without a
    merge base, as a shallow clone's origin/main does, is not passed over for
    the next: that is a local main, which may be older, and comparing with an
    older main can let an unbumped change through.

    Raises:
        GitError: git failed other than by answering no.
    """
    for candidate in [ref] if ref else ["origin/main", "main"]:
        if _resolve(root, f"{candidate}^{{commit}}") is None:
            continue
        found = _git_probe(root, ["merge-base", "HEAD", candidate])
        return _Base((found or "").strip() or None, candidate)
    return _Base(None)


def base_commit(root: Path, ref: str | None) -> str | None:
    """Find the commit to compare versions against.

    The merge base rather than the branch's tip: what reached main after this
    branch was cut is main's work, and diffing against the tip would count it
    as this branch's and demand bumps for changes the branch never made.

    Args:
        root: Repository root.
        ref: The branch, tag or commit `--base` names; None tries
            `origin/main`, then `main`.

    Returns:
        The merge base of HEAD and the first candidate that resolves, or None
        when none resolves or the one that does shares no history with HEAD.

    Raises:
        GitError: git failed other than by answering no.
    """
    return _find_base(root, ref).commit


def _no_base_problem(found: _Base, ref: str | None) -> Problem:
    """Say why versions are not compared, for a base `_find_base` did not find.

    A candidate that resolves without a merge base is an error: main is there
    to compare with, and skipping the comparison would pass an unbumped change.
    The warning is kept for a repository that has no main at all.
    """
    if found.ref:
        message = (
            f"{found.ref} shares no history with HEAD here (a shallow clone? run "
            "git fetch --unshallow): versions not compared"
        )
        return _error(REPO, message)
    if ref:
        return _error(REPO, f"--base {ref} names no commit here: versions not compared")
    return _warning(REPO, "no main to compare against: version checks skipped")


def changed_files(root: Path, base: str) -> set[str]:
    """List every path that differs from the base, committed or not.

    Args:
        root: Repository root.
        base: The commit to compare against, from `base_commit`.

    Returns:
        Repo-relative POSIX paths added, changed or deleted against the base
        in the index or the working tree, plus the untracked files git does
        not ignore. A rename lists both its old and its new path.

    Raises:
        GitError: git could not compare or list the files.
    """
    # --no-renames: with rename detection, --name-only lists only the new path.
    diff = ["diff", "--name-only", "--no-renames", "--relative", "-z"]
    files: set[str] = set()
    for args in (
        [*diff, base, "--"],
        [*diff, "--cached", base, "--"],
        ["ls-files", "-z", "--others", "--exclude-standard"],
    ):
        files.update(_git_paths(root, args))
    return files


def changed_mods(files: Iterable[str]) -> set[str]:
    """Name the mods that have any changed file, shipped or not.

    Args:
        files: Repo-relative POSIX paths, as `changed_files` returns them.

    Returns:
        The folder names under `plugins/` that hold at least one of them.
    """
    mods: set[str] = set()
    for path in files:
        parts = PurePosixPath(path).parts
        if len(parts) >= 3 and parts[0] == PLUGINS and not parts[1].startswith("."):
            mods.add(parts[1])
    return mods


def is_shipped(path: str) -> bool:
    """Decide whether a change to a path reaches the machines a mod is on.

    Args:
        path: A repo-relative POSIX path.

    Returns:
        True for a file inside `plugins/<mod>/` other than a `CHANGELOG.md`,
        `README.md` or `tsconfig.json`, a `*.test.ts(x)` file, or anything under
        the generated `.claude-plugin/types/`; False for everything else.
    """
    parts = PurePosixPath(path).parts
    if len(parts) < 3 or parts[0] != PLUGINS or parts[1].startswith("."):
        return False
    inside = parts[2:]
    if inside[-1] in NOT_SHIPPED or inside[-1].endswith(TEST_SUFFIXES):
        return False
    return not (len(inside) > 2 and inside[:2] == GENERATED)


def version_problems(
    root: Path, base: str, mods: Iterable[str], files: Iterable[str]
) -> list[Problem]:
    """Check that each mod's version moved past the base's when it had to.

    Args:
        root: Repository root.
        base: The commit to compare against, from `base_commit`.
        mods: The mods to compare. A name whose folder is gone but that had a
            `plugin.json` at the base is reported as removed.
        files: Changed paths, as `changed_files` returns them.

    Returns:
        An error for a shipped change without a higher version, or a version
        below the base's; warnings for a bump past the next patch, minor or
        major, a base version that cannot be read, and a removed mod.

    Raises:
        GitError: git could not list or read what the checks need.
    """
    changed = set(files)
    present = _present_mods(root)
    at_base = _mods_at(root, base)
    problems: list[Problem] = []

    for mod in sorted(set(mods)):
        if mod not in present:
            if mod in at_base:
                # The name machines installed it under, should it have changed.
                name = _marketplace_name(root, base)
                message = (
                    f"removed: machines that installed {mod}@{name} stop loading "
                    "it once their copy of the marketplace refreshes, and report "
                    f'"Plugin {mod} not found in marketplace {name}" until they '
                    f"run claude plugin uninstall {mod}@{name}"
                )
                problems.append(_warning(f"{PLUGINS}/{mod}", message))
            continue
        if mod not in at_base:
            continue  # a new mod: nothing on main to be above

        where = f"{PLUGINS}/{mod}/.claude-plugin/plugin.json"
        old_text, why = _version_at(root, base, mod)
        old = parse_version(old_text)
        if old is None:
            problems.append(_warning(where, f"{why}: version not compared"))
            continue
        new_text = _current_version(root, mod)
        new = parse_version(new_text)
        if new is None:
            continue  # mod_problems reports the version itself

        prefix = f"{PLUGINS}/{mod}/"
        shipped = any(path.startswith(prefix) and is_shipped(path) for path in changed)
        if new < old:
            message = f"version {new_text} is lower than main's {old_text}"
            problems.append(_error(where, message))
        elif new == old and shipped:
            message = (
                f"changed since main but version is still {new_text}; raise it "
                "once (patch, minor or major) and add its CHANGELOG.md section"
            )
            problems.append(_error(where, message))
        elif new > old and new not in _next_versions(old):
            expected = ", ".join(_format(v) for v in _next_versions(old))
            message = f"jumps from {old_text} to {new_text}; expected one of {expected}"
            problems.append(_warning(where, message))
    return problems


# --- marketplace -------------------------------------------------------------


def _usable_name(name: str) -> bool:
    """Whether an entry's name can stand for a folder under `plugins/`."""
    return name not in {"", ".", ".."} and not any(c in name for c in "/\\\0")


def _reserved_name_problems(name: str) -> list[Problem]:
    """Check a mod's name against the plugin names Claude Code keeps for itself.

    Args:
        name: The entry's kebab-case name.

    Returns:
        An error for a name the engine refuses, a warning for one it only warns
        about, or nothing.
    """
    words = name.split("-")
    official = any(
        word == "official" and BRAND_WORDS & set(words[max(0, i - 1) : i + 2])
        for i, word in enumerate(words)
    )
    if name in RESERVED_NAMES or name.startswith(RESERVED_PREFIXES) or official:
        message = f'entry "{name}": Claude Code reserves this plugin name; pick another'
        return [_error(MARKETPLACE, message, mod=name)]
    if BRAND_WORDS & set(words):
        message = (
            f'entry "{name}": Claude Code warns about a plugin name that has '
            '"claude", "anthropic" or "anthropics" as a word'
        )
        return [_warning(MARKETPLACE, message, mod=name)]
    return []


def _entry_problems(
    root: Path,
    label: str,
    entry: dict[str, object],
    entries: dict[str, dict[str, object]],
    present: set[str],
) -> list[Problem]:
    """Check one marketplace entry, recording it in `entries` when it is usable.

    Args:
        root: Repository root.
        label: How messages name the entry before its name is known.
        entry: The entry.
        entries: Entries accepted so far, by name; updated in place.
        present: Mod folders on disk, from `_present_mods`.

    Returns:
        The entry's problems, each marked as the named mod's.
    """
    name = entry.get("name")
    if name is None:
        return [_error(MARKETPLACE, f"{label}: name is missing")]
    problems: list[Problem] = []
    if not isinstance(name, str) or not KEBAB.fullmatch(name):
        message = f"{label}: name {_show(name)} is not kebab-case"
        mod = name if isinstance(name, str) else ""
        problems.append(_error(MARKETPLACE, message, mod=mod))
        if not isinstance(name, str) or not _usable_name(name):
            return problems
    if name in entries:
        message = f'{label}: "{name}" is listed twice'
        problems.append(_error(MARKETPLACE, message, mod=name))
        return problems
    entries[name] = entry
    problems += _reserved_name_problems(name)

    expected = f"./{PLUGINS}/{name}"
    source = entry.get("source")
    if source != expected:
        found = "missing" if source is None else _show(source)
        message = f'entry "{name}": source is {found}; it must be "{expected}"'
        problems.append(_error(MARKETPLACE, message, mod=name))
    if "version" in entry:
        message = f'entry "{name}" has a version: version lives in plugin.json only'
        problems.append(_error(MARKETPLACE, message, mod=name))
    if name not in present:
        message = f'entry "{name}": {PLUGINS}/{name} is missing or empty'
        problems.append(_error(MARKETPLACE, message, mod=name))
    elif "description" in entry:
        manifest = f"{PLUGINS}/{name}/.claude-plugin/plugin.json"
        data = _read_json(root / manifest).data
        if isinstance(data, dict) and entry["description"] != data.get("description"):
            message = (
                f'entry "{name}": description differs from {manifest}\'s; make '
                "them the same, or drop it here"
            )
            problems.append(_error(MARKETPLACE, message, mod=name))
    return problems


def _kebab(name: str) -> str:
    """A kebab-case name close to `name`, to suggest renaming it to."""
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name).lower()
    return "-".join(re.findall(r"[a-z0-9]+", words)) or "my-mod"


def load_marketplace(root: Path) -> tuple[Marketplace | None, list[Problem]]:
    """Read and check `.claude-plugin/marketplace.json`.

    Args:
        root: Repository root.

    Returns:
        The marketplace, or None when the file is missing, unreadable or not a
        JSON object; and every problem found in it, including folders under
        `plugins/` that no entry lists, and a git failure while listing them,
        after which every folder counts.
    """
    loaded = _read_json(root / MARKETPLACE)
    if not loaded.found:
        message = "missing: without it this repo is not a marketplace"
        return None, [_error(MARKETPLACE, message)]
    problems = [_warning(MARKETPLACE, BOM)] if loaded.bom else []
    if loaded.error:
        return None, [*problems, _error(MARKETPLACE, loaded.error)]
    if not isinstance(loaded.data, dict):
        return None, [*problems, _error(MARKETPLACE, "must be a JSON object")]
    data = loaded.data

    name = data.get("name")
    if name is None:
        problems.append(_error(MARKETPLACE, "name is missing"))
    elif not isinstance(name, str) or not KEBAB.fullmatch(name):
        problems.append(_error(MARKETPLACE, f"name {_show(name)} is not kebab-case"))
    name = name if isinstance(name, str) else ""

    owner = data.get("owner")
    owner_name = owner.get("name") if isinstance(owner, dict) else None
    if not isinstance(owner_name, str) or not owner_name.strip():
        message = 'owner must be an object with a non-empty "name"'
        problems.append(_error(MARKETPLACE, message))

    # The engine reads `metadata.description` where the top-level one is absent.
    description = data.get("description")
    metadata = data.get("metadata")
    if description is None and isinstance(metadata, dict):
        description = metadata.get("description")
    if not isinstance(description, str) or not description.strip():
        message = f"description is missing or empty: {STRICT}"
        problems.append(_error(MARKETPLACE, message))

    plugins = data.get("plugins")
    if not isinstance(plugins, list):
        problems.append(_error(MARKETPLACE, '"plugins" must be a list of entries'))
        return Marketplace(name), problems

    entries: dict[str, dict[str, object]] = {}
    present, found = _mods_on_disk(root)
    problems += found
    for index, entry in enumerate(plugins):
        label = f"plugins[{index}]"
        if isinstance(entry, dict):
            problems += _entry_problems(root, label, entry, entries, present)
        else:
            problems.append(_error(MARKETPLACE, f"{label} must be an object"))

    for folder in sorted(present - set(entries)):
        if KEBAB.fullmatch(folder):
            message = (
                f'no marketplace entry lists this folder; add {{ "name": "{folder}", '
                f'"source": "./{PLUGINS}/{folder}" }} to {MARKETPLACE}'
            )
        else:
            # An entry by this name would only be refused as not kebab-case.
            message = (
                "folder name is not kebab-case: rename the folder and "
                f'plugin.json\'s name, such as "{_kebab(folder)}"'
            )
        problems.append(_error(f"{PLUGINS}/{folder}", message))
    return Marketplace(name, entries), problems


# --- one mod -----------------------------------------------------------------


def _manifest_problems(folder: Path, name: str) -> tuple[list[Problem], str | None]:
    """Check a mod's `plugin.json`.

    Args:
        folder: The mod's folder.
        name: The folder's name, which the manifest's `name` must equal.

    Returns:
        The problems, and the version when it is a valid X.Y.Z.
    """
    where = f"{PLUGINS}/{name}/.claude-plugin/plugin.json"
    loaded = _read_json(folder / ".claude-plugin" / "plugin.json")
    if not loaded.found:
        return [_error(where, "missing")], None
    problems = [_warning(where, BOM)] if loaded.bom else []
    if loaded.error:
        return [*problems, _error(where, loaded.error)], None
    if not isinstance(loaded.data, dict):
        return [*problems, _error(where, "must be a JSON object")], None
    data = loaded.data

    if data.get("name") != name:
        found = "missing" if data.get("name") is None else _show(data.get("name"))
        message = f'name is {found}; it must be the folder\'s name, "{name}"'
        problems.append(_error(where, message))

    version = data.get("version")
    if version is None:
        message = 'version is missing; it must be X.Y.Z, such as "0.1.0"'
        problems.append(_error(where, message))
        version = None
    elif not isinstance(version, str) or parse_version(version) is None:
        message = (
            f"version {_show(version)} is not X.Y.Z: three whole numbers without "
            'leading zeros, no prerelease, no leading "v"'
        )
        problems.append(_error(where, message))
        version = None

    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        problems.append(_error(where, "description is missing or empty"))

    author = data.get("author")
    author_name = author.get("name") if isinstance(author, dict) else None
    if not isinstance(author_name, str) or not author_name.strip():
        message = (
            'author must be an object with a non-empty "name", such as '
            f'{{ "name": "Peter" }}: {STRICT}'
        )
        problems.append(_error(where, message))

    if "types" in data:
        types = data["types"]
        if not isinstance(types, str) or not types.strip():
            message = (
                "types must be the path of the $.state contract, such as "
                '"./types/index.d.ts"'
            )
            problems.append(_error(where, message))
        elif not types.startswith("./"):
            # Claude Code's own rule, and its install refuses the mod for it.
            message = 'types must start with "./", such as "./types/index.d.ts"'
            problems.append(_error(where, message))
        elif not _inside(folder, types):
            message = f"types names {_show(types)}, which is not a file in the mod"
            problems.append(_error(where, message))

    return problems, version


def _hooks_problems(folder: Path, name: str) -> list[Problem]:
    """Check a mod's `hooks/hooks.json` and that the module it names exists.

    Claude Code loads one hooks module per mod. A mod that names two installs
    and then fails to load, which only `claude plugin validate` would say, so
    the quick half has to say it too.
    """
    where = f"{PLUGINS}/{name}/hooks/hooks.json"
    loaded = _read_json(folder / "hooks" / "hooks.json")
    if not loaded.found:
        message = "missing: not a mod; fine for a plugin of skills or commands"
        return [_warning(where, message)]
    problems = [_warning(where, BOM)] if loaded.bom else []
    if loaded.error:
        return [*problems, _error(where, loaded.error)]
    if not isinstance(loaded.data, dict):
        return [*problems, _error(where, "must be a JSON object")]

    modules = loaded.data.get("modules")
    if (
        not isinstance(modules, list)
        or len(modules) != 1
        or not isinstance(modules[0], str)
        or not modules[0].strip()
    ):
        message = (
            '"modules" must name exactly one hooks module, such as ["./register.tsx"]'
        )
        return [*problems, _error(where, message)]
    module = modules[0]
    # The path is relative to hooks/, and may reach anywhere in the mod.
    if not _inside(folder, f"hooks/{module}"):
        message = (
            f"module {module} is not a file in the mod (module paths are "
            "relative to hooks/)"
        )
        problems.append(_error(where, message))
    return problems


def _changelog_problems(folder: Path, name: str, version: str | None) -> list[Problem]:
    """Check that `CHANGELOG.md` exists and opens with the current version."""
    where = f"{PLUGINS}/{name}/CHANGELOG.md"
    section = f"## {version or 'X.Y.Z'} — YYYY-MM-DD"
    text = _read_text(folder / "CHANGELOG.md")
    if text is None:
        message = f"missing: add one, newest version first, starting {section}"
        return [_error(where, message)]
    if version is None:
        return []  # the manifest's error says what is wrong with the version

    headings = [bracket or plain for bracket, plain in CHANGELOG_HEADING.findall(text)]
    if version not in headings:
        message = f"no CHANGELOG.md section for {version}: add {section} at the top"
        return [_error(where, message)]
    if headings[0] != version:
        message = (
            f"the first section is {headings[0]}, not {version}: the newest "
            "version comes first"
        )
        return [_warning(where, message)]
    return []


def mod_problems(root: Path, name: str) -> list[Problem]:
    """Check one mod's folder: manifest, hooks and changelog.

    Args:
        root: Repository root.
        name: The mod, which is also its folder's name under `plugins/`.

    Returns:
        Every problem in the folder; one error when the folder does not exist.
    """
    folder = root / PLUGINS / name
    if not folder.is_dir():
        return [_error(f"{PLUGINS}/{name}", "no such folder")]
    problems, version = _manifest_problems(folder, name)
    problems += _hooks_problems(folder, name)
    problems += _changelog_problems(folder, name, version)
    return problems


# --- whole repo --------------------------------------------------------------


def readme_problems(
    root: Path, marketplace_name: str, mods: Iterable[str]
) -> list[Problem]:
    """Check that the README's install lines and the mods agree.

    Args:
        root: Repository root.
        marketplace_name: The marketplace's name; empty checks only that the
            README exists.
        mods: Every mod there is. A name that is not kebab-case is left out:
            no mention could match it, and its own error says to rename it.

    Returns:
        An error for a missing README, for each mod it never names as
        `<mod>@<marketplace>` where a reader sees it, and for each
        `<word>@<marketplace>` naming no mod.
    """
    text = _read_text(root / README)
    if text is None:
        return [_error(README, "missing: it lists every mod with its install line")]
    if not marketplace_name:
        return []

    # Only what the README shows counts: an HTML comment shows nothing, and
    # one left open hides the rest of the file.
    shown = re.sub(r"<!--.*?(?:-->|\Z)", "", text, flags=re.DOTALL)
    at = re.escape(marketplace_name)
    # A period may end the sentence after a mention, but not carry on into a
    # domain: peter@claude-mods.dev names no mod.
    mention = re.compile(
        rf"(?<![A-Za-z0-9_-])({KEBAB.pattern})@{at}(?![A-Za-z0-9_-]|\.[A-Za-z0-9])"
    )
    named = set(mention.findall(shown))
    known = {mod for mod in mods if KEBAB.fullmatch(mod)}
    problems = []
    for mod in sorted(known - named):
        install = f"{mod}@{marketplace_name}"
        message = (
            f"does not mention {install}: add its row and its install line, "
            f"claude plugin install {install}"
        )
        problems.append(_error(README, message, mod=mod))
    for word in sorted(named - known):
        message = f"names {word}@{marketplace_name}, but no mod is called {word}"
        problems.append(_error(README, message))
    return problems


def generated_problems(root: Path) -> list[Problem]:
    """Find generated types that git tracks or has staged.

    Args:
        root: Repository root.

    Returns:
        One error per mod with any file under `.claude-plugin/types/` in the
        index; none outside git, where nothing is tracked.

    Raises:
        GitError: git could not list a repository's index.
    """
    tracked = _listing(root, ["ls-files", "-z", "--cached", "--", PLUGINS]) or []
    counts: dict[str, int] = {}
    for path in tracked:
        parts = PurePosixPath(path).parts
        if len(parts) >= 5 and parts[0] == PLUGINS and parts[2:4] == GENERATED:
            counts[parts[1]] = counts.get(parts[1], 0) + 1

    problems = []
    for mod, count in sorted(counts.items()):
        types = f"{PLUGINS}/{mod}/.claude-plugin/types"
        files = "file" if count == 1 else "files"
        message = (
            f"generated types are tracked ({count} {files}); Claude Code writes "
            f"them when it loads the mod with --plugin-dir: git rm --cached -r "
            f"{types}"
        )
        problems.append(_error(types, message))
    return problems


# --- the claude CLI ----------------------------------------------------------


def _tail(output: str) -> str:
    """The last lines of a command's output, indented under its problem line."""
    text = ANSI.sub("", output)
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        return " no output"
    return "".join(f"\n    {line}" for line in lines[-TAIL_LINES:])


def _cli_step(claude: str, root: Path, args: list[str], where: str) -> list[Problem]:
    """Run one `claude` command; an error when it fails, quoting its output."""
    label = " ".join(["claude", *args])
    try:
        result = _run([claude, *args], root, CLI_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return [_error(where, f"{label} timed out after {CLI_TIMEOUT_SECONDS} s")]
    except OSError as exc:
        return [_error(where, f"{label} could not run: {exc}")]
    if result.returncode == 0:
        return []
    output = (result.stdout or "") + (result.stderr or "")
    message = f"{label} failed (exit {result.returncode}):{_tail(output)}"
    return [_error(where, message)]


def _cli_version(claude: str, root: Path) -> str:
    """The CLI's version, as `claude --version` reports it, or "unknown"."""
    try:
        result = _run([claude, "--version"], root, CLI_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    match = CLI_VERSION.search(result.stdout or "")
    return match.group(0) if match else "unknown"


def _has_tests(folder: Path) -> bool:
    return any(
        path.is_file()
        for suffix in TEST_SUFFIXES
        for path in folder.rglob(f"*{suffix}")
    )


def cli_problems(root: Path, mods: Iterable[str]) -> tuple[list[Problem], str | None]:
    """Validate the marketplace and the mods, and run their tests, with the CLI.

    On Windows `claude` is a `.cmd` shim, so cmd.exe parses each command line:
    paths go in relative to `root`, and a mod whose name is not kebab-case,
    and so could carry shell syntax, is skipped. Its name is an error already.

    Args:
        root: Repository root.
        mods: The mods to validate; one with no folder is skipped.

    Returns:
        The problems, and the CLI's version: None when the CLI is not on PATH,
        "unknown" when it runs but does not say.
    """
    claude = shutil.which("claude")
    if claude is None:
        message = (
            "claude CLI not on PATH; nothing was validated (use --no-cli to skip "
            "deliberately)"
        )
        return [_error(REPO, message)], None

    version = _cli_version(claude, root)
    problems: list[Problem] = []
    if (root / MARKETPLACE).is_file():
        args = ["plugin", "validate", *VALIDATE_FLAGS, "."]
        problems += _cli_step(claude, root, args, MARKETPLACE)
    for mod in sorted(set(mods)):
        folder = root / PLUGINS / mod
        if not KEBAB.fullmatch(mod) or not folder.is_dir():
            continue
        relative = f"{PLUGINS}/{mod}"
        args = ["plugin", "validate", *VALIDATE_FLAGS, relative]
        problems += _cli_step(claude, root, args, relative)
        if _has_tests(folder):
            problems += _cli_step(claude, root, ["plugin", "test", relative], relative)
    return problems, version


# --- the run -----------------------------------------------------------------


@dataclass(frozen=True)
class _History:
    """What git says about the branch, for the version checks.

    Attributes:
        base: The commit to compare against.
        files: The paths changed since, as `changed_files` returns them.
        at_base: The mods that had a `plugin.json` at the base.
    """

    base: str
    files: set[str]
    at_base: set[str]


def _history(root: Path, base_ref: str | None) -> tuple[_History | None, list[Problem]]:
    """Ask git for the base and for what changed since.

    Args:
        root: Repository root.
        base_ref: `--base`, or None for origin/main, else main.

    Returns:
        The history, or None when versions cannot be compared; and why not:
        no repository, no base, or a git command that failed.
    """
    try:
        if not _is_git_repo(root):
            message = (
                "not a git repository, or git is not installed: the version, "
                "--changed and generated-types checks did not run"
            )
            return None, [_error(REPO, message)]
        found = _find_base(root, base_ref)
        if found.commit is None:
            return None, [_no_base_problem(found, base_ref)]
        files = changed_files(root, found.commit)
        at_base = _mods_at(root, found.commit)
    except GitError as exc:
        return None, [_error(REPO, str(exc))]
    return _History(found.commit, files, at_base), []


def _ok_line(root: Path, mod: str, main: str, errors: list[Problem]) -> str | None:
    """The `ok:` line for a mod, or None when it has an error or no version.

    Args:
        root: Repository root.
        mod: The mod.
        main: What to say of main's version: the version, "new", "unreadable"
            or "not compared".
        errors: Every error the run found. One belongs to the mod when it is
            in the mod's folder or names the mod as its own.
    """
    folder = f"{PLUGINS}/{mod}"
    if any(
        p.mod == mod or p.where == folder or p.where.startswith(folder + "/")
        for p in errors
    ):
        return None
    version = _current_version(root, mod)
    if version is None:
        return None
    return f"ok: {mod} {version} (main: {main})"


def _check(
    root: Path,
    mods: list[str] | None,
    changed: bool,
    no_cli: bool,
    base_ref: str | None,
) -> _Report:
    """Run every check the arguments ask for.

    A git failure is one of the errors, and the checks that needed its answer
    are left out rather than given an empty one: `--changed` then selects every
    mod, and the `ok:` lines say main was not compared.

    Args:
        root: Repository root.
        mods: The `--mod` names, or None for every mod.
        changed: `--changed`: only mods with a changed file.
        no_cli: `--no-cli`: skip `claude plugin validate` and `test`.
        base_ref: `--base`, or None for origin/main, else main.

    Returns:
        The problems, the `ok:` lines and the CLI's version.
    """
    marketplace, problems = load_marketplace(root)
    present, found = _mods_on_disk(root)
    problems += found
    known = present | (set(marketplace.entries) if marketplace else set())
    name = marketplace.name if marketplace else ""

    history, found = _history(root, base_ref)
    problems += found
    # Had a plugin.json at the base, and has no folder now.
    gone = history.at_base - present if history else set()

    selected = set(known)
    if mods is not None:
        wanted = set(mods)
        for missing in sorted(wanted - known - gone):
            message = f"no mod named {missing}: no folder and no marketplace entry"
            problems.append(_error(f"{PLUGINS}/{missing}", message))
        selected &= wanted
        gone &= wanted
    if changed and history:
        touched = changed_mods(history.files)
        selected &= touched
        gone &= touched
    checked = sorted(selected & present)

    for mod in checked:
        problems += mod_problems(root, mod)

    mains = dict.fromkeys(checked, "not compared")
    if history:
        try:
            files = history.files
            problems += version_problems(root, history.base, selected | gone, files)
            problems += _rename_problems(root, history.base, name)
            for mod in checked:
                if mod not in history.at_base:
                    mains[mod] = "new"
                else:
                    old = _version_at(root, history.base, mod)[0]
                    mains[mod] = old or "unreadable"
        except GitError as exc:
            problems.append(_error(REPO, str(exc)))
            mains = dict.fromkeys(checked, "not compared")
    problems += readme_problems(root, name, known)
    try:
        problems += generated_problems(root)
    except GitError as exc:
        problems.append(_error(REPO, str(exc)))

    cli_version = None
    if not no_cli:
        found, cli_version = cli_problems(root, checked)
        problems += found

    problems = list(dict.fromkeys(problems))  # one cause can be reached twice
    errors = [p for p in problems if p.level == ERROR]
    oks = [_ok_line(root, mod, mains[mod], errors) for mod in checked]
    return _Report(problems, [line for line in oks if line], cli_version)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_mods.py",
        description=(
            "Check the marketplace and its mods: marketplace.json and every "
            "plugin.json agree, each changed mod's version is raised above main's "
            "with a changelog section, the README lists every mod, generated types "
            "are not tracked, and claude plugin validate and the mods' tests pass."
        ),
        epilog=(
            "Exit status: 0 without errors (warnings allowed), 1 with any, 2 for "
            "a usage error."
        ),
    )
    parser.add_argument(
        "--mod",
        action="append",
        metavar="NAME",
        help="check only this mod; repeatable. The marketplace, README and "
        "generated-types checks still cover the whole repo",
    )
    parser.add_argument(
        "--changed",
        action="store_true",
        help="check only mods with a file changed against the base, committed or "
        "not (every mod when there is no base)",
    )
    parser.add_argument(
        "--no-cli",
        action="store_true",
        help="skip claude plugin validate and claude plugin test",
    )
    parser.add_argument(
        "--base",
        metavar="REF",
        help="compare versions against the merge base of HEAD and REF "
        "(default: origin/main, else main)",
    )
    parser.add_argument(
        "--root",
        metavar="PATH",
        help="the repo to check (default: the folder above scripts/)",
    )
    return parser


def _utf8_output() -> None:
    """Print UTF-8 whatever the console's code page; never fail on a character."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    """Run the checks and print the report.

    Args:
        argv: Arguments without the program's name; None reads `sys.argv`.

    Returns:
        The exit status: 0 without errors, 1 with any, 2 for a usage error.
    """
    _utf8_output()
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        default = Path(__file__).resolve().parents[1]
        root = Path(args.root).resolve() if args.root else default
        if not root.is_dir():
            parser.error(f"--root {args.root}: no such directory")
    except SystemExit as exc:  # argparse exits for --help and for usage errors
        return exc.code if isinstance(exc.code, int) else 2

    report = _check(root, args.mod, args.changed, args.no_cli, args.base)
    errors = [p for p in report.problems if p.level == ERROR]
    warnings = [p for p in report.problems if p.level == WARNING]
    for problem in [*errors, *warnings]:
        print(problem)
    for line in report.oks:
        print(line)
    print(f"{len(errors)} error(s), {len(warnings)} warning(s)")
    if report.cli_version == "unknown":
        print("validated with Claude Code (version unknown)")
    elif report.cli_version is not None:
        print(f"validated with Claude Code {report.cli_version}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
