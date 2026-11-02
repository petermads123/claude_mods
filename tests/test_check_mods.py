"""Tests for scripts/check_mods.py.

Every check runs in both directions: silent when the repo is right, and the
right level at the right place when it is wrong. The version gate runs against
real temporary git repositories, because what counts as changed is git's
answer. The claude CLI is never called: `cli_problems` runs against a mocked
`shutil.which` and `subprocess.run`, and every other call to `main` passes
`--no-cli`.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_mods.py"


def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_mods", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    # dataclasses look their module up in sys.modules while a class is built.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check_mods = _load(SCRIPT)
MARKETPLACE = check_mods.MARKETPLACE
ERROR = check_mods.ERROR
WARNING = check_mods.WARNING
# Captured before any test patches subprocess.run, so test git calls stay real.
REAL_RUN = subprocess.run
# The checker's own runner, for a stand-in that fails only the calls it names.
CHECKER_RUN = check_mods._run

GIT_ENV = {
    key: value
    for key, value in os.environ.items()
    if key not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}
}
GIT_ENV.update(
    GIT_AUTHOR_NAME="Check Mods Test",
    GIT_AUTHOR_EMAIL="check-mods@example.invalid",
    GIT_COMMITTER_NAME="Check Mods Test",
    GIT_COMMITTER_EMAIL="check-mods@example.invalid",
)


def git(root: Path, *args: str) -> str:
    """Run git in a test repo, failing the test when git fails."""
    result = REAL_RUN(
        ["git", "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", *args],
        cwd=root,
        env=GIT_ENV,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed:\n{result.stderr}")
    return result.stdout.strip()


def write(root: Path, relative: str, content: str | bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    return path


def write_json(root: Path, relative: str, data: object) -> Path:
    return write(root, relative, json.dumps(data, indent=2) + "\n")


def read_json(root: Path, relative: str) -> dict[str, Any]:
    return json.loads((root / relative).read_text(encoding="utf-8"))


def append(root: Path, relative: str, text: str) -> None:
    write(root, relative, (root / relative).read_text(encoding="utf-8") + text)


def manifest(mod: str) -> str:
    return f"plugins/{mod}/.claude-plugin/plugin.json"


def without_ellipsis(data: dict[str, Any]) -> dict[str, Any]:
    """Drop the keys a test set to `...`, its way of saying "leave it out"."""
    return {key: value for key, value in data.items() if value is not ...}


def make_mod(root: Path, mod: str, version: str = "0.1.0") -> None:
    """Write a mod that passes every per-mod check."""
    data = {
        "name": mod,
        "version": version,
        "description": f"The {mod} mod",
        "author": {"name": "Tester"},
    }
    write_json(root, manifest(mod), data)
    hooks = {"modules": ["./register.tsx"]}
    write_json(root, f"plugins/{mod}/hooks/hooks.json", hooks)
    code = "export const register = () => {}\n"
    write(root, f"plugins/{mod}/hooks/register.tsx", code)
    changelog = f"# {mod}\n\n## {version} — 2026-11-02\n\n- First.\n"
    write(root, f"plugins/{mod}/CHANGELOG.md", changelog)


def make_marketplace(
    root: Path, mods: Iterable[str], name: str = "claude-mods"
) -> None:
    entries = [{"name": mod, "source": f"./plugins/{mod}"} for mod in mods]
    data = {
        "name": name,
        "description": "Test mods",
        "owner": {"name": "Tester"},
        "plugins": entries,
    }
    write_json(root, MARKETPLACE, data)


def make_readme(
    root: Path, mods: Iterable[str], marketplace: str = "claude-mods"
) -> None:
    rows = "".join(f"- `claude plugin install {mod}@{marketplace}`\n" for mod in mods)
    write(root, "README.md", f"# Mods\n\n{rows}")


def make_repo(root: Path, mods: dict[str, str]) -> None:
    """Write a marketplace, its README and its mods, all in order."""
    for mod, version in mods.items():
        make_mod(root, mod, version)
    make_marketplace(root, mods)
    make_readme(root, mods)


def bump(root: Path, mod: str, version: str) -> None:
    """Raise a mod's version as /mod does: plugin.json and a changelog section."""
    data = read_json(root, manifest(mod))
    data["version"] = version
    write_json(root, manifest(mod), data)
    changelog = f"plugins/{mod}/CHANGELOG.md"
    text = (root / changelog).read_text(encoding="utf-8")
    title, _, rest = text.partition("\n\n")
    section = f"## {version} — 2026-11-03\n\n- Bumped.\n\n"
    write(root, changelog, f"{title}\n\n{section}{rest}")


def edit(root: Path, mod: str) -> None:
    """Change code the mod ships."""
    append(root, f"plugins/{mod}/hooks/register.tsx", "// changed\n")


def output_lines(output: str, level: str) -> list[str]:
    return [line for line in output.splitlines() if line.startswith(f"{level}: ")]


@contextlib.contextmanager
def git_failing(
    *prefix: str, code: int = 128, error: BaseException | None = None
) -> Iterator[None]:
    """Make the checker's git commands that start with `prefix` fail.

    Each such command exits with `code` and a fatal line on stderr, or raises
    `error` when one is given; every other command runs for real.
    """

    def run(
        cmd: list[str], cwd: Path, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "git" and tuple(cmd[1 : 1 + len(prefix)]) == prefix:
            if error is not None:
                raise error
            return subprocess.CompletedProcess(cmd, code, "", "fatal: simulated\n")
        return CHECKER_RUN(cmd, cwd, timeout)

    with mock.patch.object(check_mods, "_run", run):
        yield


class TempDirTest(unittest.TestCase):
    """A fresh directory per test, which git never finds a repository above."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        ceiling = {"GIT_CEILING_DIRECTORIES": str(self.root.parent)}
        patcher = mock.patch.dict(os.environ, ceiling)
        patcher.start()
        self.addCleanup(patcher.stop)

    def check(self, *args: str) -> tuple[int, str]:
        """Run main on this root without the CLI; its exit code and stdout."""
        return self.run_main("--root", str(self.root), "--no-cli", *args)

    def run_main(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = check_mods.main(list(args))
        return code, out.getvalue()

    def assert_found(
        self, problems: list[Any], level: str, where: str, fragment: str
    ) -> None:
        if not any(
            p.level == level and p.where == where and fragment in p.message
            for p in problems
        ):
            shown = "\n".join(str(p) for p in problems) or "(none)"
            self.fail(f"no {level} at {where} saying {fragment!r}; found:\n{shown}")

    def assert_clean(self, problems: list[Any]) -> None:
        self.assertEqual([], [str(p) for p in problems])


class GitRepoTest(TempDirTest):
    """A repo whose main holds alpha 0.1.0 and beta 1.2.3, on a work branch."""

    MODS: ClassVar[dict[str, str]] = {"alpha": "0.1.0", "beta": "1.2.3"}

    def setUp(self) -> None:
        super().setUp()
        git(self.root, "init", "-q")
        git(self.root, "config", "core.autocrlf", "false")
        git(self.root, "config", "commit.gpgsign", "false")
        write(self.root, ".gitignore", "plugins/*/.claude-plugin/types/\n")
        make_repo(self.root, self.MODS)
        self.prepare()
        self.first = self.commit("first versions")
        git(self.root, "checkout", "-q", "-b", "feat/work")

    def prepare(self) -> None:
        """Change main's first commit; subclasses override."""

    def commit(self, message: str = "work") -> str:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "--no-verify", "-m", message)
        return git(self.root, "rev-parse", "HEAD")

    def base(self) -> str:
        base = check_mods.base_commit(self.root, None)
        if base is None:
            self.fail("no base commit")
        return base

    def versions(self, *mods: str) -> list[Any]:
        """version_problems for these mods (all, by default) against main."""
        base = self.base()
        files = check_mods.changed_files(self.root, base)
        return check_mods.version_problems(self.root, base, mods or self.MODS, files)


# --- Problem and parse_version -----------------------------------------------


class ProblemTest(unittest.TestCase):
    def test_renders_level_where_and_message(self) -> None:
        problem = check_mods.Problem("error", "plugins/alpha", "something is wrong")
        self.assertEqual("error: plugins/alpha: something is wrong", str(problem))

    def test_is_frozen(self) -> None:
        problem = check_mods.Problem("warning", ".", "x")
        with self.assertRaises(AttributeError):
            problem.level = "error"

    def test_the_mod_it_belongs_to_is_neither_printed_nor_compared(self) -> None:
        owned = check_mods.Problem("error", MARKETPLACE, "x", "alpha")
        plain = check_mods.Problem("error", MARKETPLACE, "x")
        self.assertEqual("alpha", owned.mod)
        self.assertEqual("", plain.mod)
        self.assertEqual(f"error: {MARKETPLACE}: x", str(owned))
        self.assertEqual(plain, owned)
        self.assertEqual(1, len({plain, owned}))


class ParseVersionTest(unittest.TestCase):
    def test_reads_three_whole_numbers(self) -> None:
        cases = {
            "0.1.0": (0, 1, 0),
            "1.0.0": (1, 0, 0),
            "10.20.300": (10, 20, 300),
            "10.0.10": (10, 0, 10),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(expected, check_mods.parse_version(text))

    def test_rejects_anything_else(self) -> None:
        for value in (
            "v1.0.0",
            "1.0",
            "1",
            "1.0.0-rc.1",
            "1.0.0+build.5",
            "1.0.0.0",
            "01.0.0",
            "0.01.0",
            "0.0.01",
            "00.0.0",
            " 1.0.0",
            "1.0.0 ",
            "1.0.0\n",
            "",
            "a.b.c",
            "١.٠.٠",
            None,
            1,
            1.0,
            True,
            ["1", "0", "0"],
        ):
            with self.subTest(value=value):
                self.assertIsNone(check_mods.parse_version(value))


# --- is_shipped and changed_mods ---------------------------------------------


class ShippedTest(unittest.TestCase):
    def test_what_a_mod_ships(self) -> None:
        for path in (
            "plugins/alpha/hooks/register.tsx",
            "plugins/alpha/hooks/hooks.json",
            "plugins/alpha/.claude-plugin/plugin.json",
            "plugins/alpha/types/index.d.ts",
            "plugins/alpha/assets/logo.png",
            "plugins/alpha/.claude-plugin/types.json",
        ):
            with self.subTest(path=path):
                self.assertTrue(check_mods.is_shipped(path))

    def test_what_a_mod_does_not_ship(self) -> None:
        for path in (
            "plugins/alpha/CHANGELOG.md",
            "plugins/alpha/README.md",
            "plugins/alpha/docs/README.md",
            "plugins/alpha/tsconfig.json",
            "plugins/alpha/hooks/register.test.ts",
            "plugins/alpha/hooks/ui/view.test.tsx",
            "plugins/alpha/.claude-plugin/types/index.d.ts",
            "plugins/alpha/.claude-plugin/types/nested/state.d.ts",
        ):
            with self.subTest(path=path):
                self.assertFalse(check_mods.is_shipped(path))

    def test_paths_outside_a_mod_are_not_shipped(self) -> None:
        for path in (
            "README.md",
            ".claude-plugin/marketplace.json",
            "scripts/check_mods.py",
            "plugins/README.md",
            "plugins/alpha",
            "plugins/.cache/state.tsx",
        ):
            with self.subTest(path=path):
                self.assertFalse(check_mods.is_shipped(path))

    def test_changed_mods_names_each_mod_with_any_changed_file(self) -> None:
        files = [
            "plugins/alpha/hooks/register.tsx",
            "plugins/alpha/CHANGELOG.md",
            "plugins/beta/.claude-plugin/types/index.d.ts",
            "plugins/README.md",
            "plugins/.cache/state.tsx",
            "README.md",
            MARKETPLACE,
        ]
        self.assertEqual({"alpha", "beta"}, check_mods.changed_mods(files))

    def test_changed_mods_of_nothing_is_empty(self) -> None:
        self.assertEqual(set(), check_mods.changed_mods([]))


# --- load_marketplace --------------------------------------------------------


class MarketplaceTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        make_repo(self.root, {"alpha": "0.1.0"})

    def load(self) -> tuple[Any, list[Any]]:
        return check_mods.load_marketplace(self.root)

    def expect(self, fragment: str, where: str = MARKETPLACE) -> None:
        """Assert the marketplace now draws an error saying `fragment`."""
        self.assert_found(self.load()[1], ERROR, where, fragment)

    def set_marketplace(self, **fields: object) -> None:
        data = read_json(self.root, MARKETPLACE)
        data.update(fields)
        write_json(self.root, MARKETPLACE, without_ellipsis(data))

    def set_entry(self, **fields: object) -> None:
        entry = {"name": "alpha", "source": "./plugins/alpha", **fields}
        self.set_marketplace(plugins=[without_ellipsis(entry)])

    def test_a_valid_marketplace_has_no_problems(self) -> None:
        marketplace, problems = self.load()
        self.assert_clean(problems)
        self.assertEqual("claude-mods", marketplace.name)
        self.assertEqual(["alpha"], list(marketplace.entries))

    def test_missing_file_is_an_error(self) -> None:
        (self.root / MARKETPLACE).unlink()
        marketplace, problems = self.load()
        self.assertIsNone(marketplace)
        self.assert_found(problems, ERROR, MARKETPLACE, "missing")

    def test_unparseable_file_is_an_error(self) -> None:
        for content, fragment in (
            (b'{ "name": "claude-mods", ', "not valid JSON"),
            (b"\xff\xfe{\x00}\x00", "UTF-16"),
            (b'{"name": "\xff"}', "not UTF-8"),
        ):
            with self.subTest(fragment=fragment):
                write(self.root, MARKETPLACE, content)
                marketplace, problems = self.load()
                self.assertIsNone(marketplace)
                self.assert_found(problems, ERROR, MARKETPLACE, fragment)

    def test_json_that_is_not_an_object_is_an_error(self) -> None:
        write_json(self.root, MARKETPLACE, [{"name": "alpha"}])
        marketplace, problems = self.load()
        self.assertIsNone(marketplace)
        self.assert_found(problems, ERROR, MARKETPLACE, "must be a JSON object")

    def test_byte_order_mark_is_a_warning_and_still_parses(self) -> None:
        text = (self.root / MARKETPLACE).read_bytes()
        write(self.root, MARKETPLACE, b"\xef\xbb\xbf" + text)
        marketplace, problems = self.load()
        self.assertIsNotNone(marketplace)
        self.assertEqual([WARNING], [p.level for p in problems])
        self.assert_found(problems, WARNING, MARKETPLACE, check_mods.BOM)

    def test_name_must_be_kebab_case(self) -> None:
        for name in ("Claude Mods", "claude_mods", "-mods", "mods-", "Mods", "", 5):
            with self.subTest(name=name):
                self.set_marketplace(name=name)
                self.expect("is not kebab-case")

    def test_name_must_be_present(self) -> None:
        self.set_marketplace(name=...)
        marketplace, problems = self.load()
        self.assert_found(problems, ERROR, MARKETPLACE, "name is missing")
        self.assertEqual("", marketplace.name)

    def test_owner_needs_a_name(self) -> None:
        for owner in ("Peter", {}, {"name": ""}, {"name": "  "}, {"name": 5}, ...):
            with self.subTest(owner=owner):
                self.set_marketplace(owner=owner)
                self.expect("owner must be")

    def test_description_must_be_a_non_empty_string(self) -> None:
        for description in ("", "   ", 5, ...):
            with self.subTest(description=description):
                self.set_marketplace(description=description)
                self.expect("description is missing or empty")

    def test_description_may_sit_in_metadata(self) -> None:
        self.set_marketplace(description=..., metadata={"description": "Test mods"})
        self.assert_clean(self.load()[1])
        self.set_marketplace(metadata={"description": ""})
        self.expect("description is missing or empty")

    def test_plugins_must_be_a_list(self) -> None:
        for plugins in ({}, "alpha", None, ...):
            with self.subTest(plugins=plugins):
                self.set_marketplace(plugins=plugins)
                marketplace, problems = self.load()
                self.assert_found(problems, ERROR, MARKETPLACE, "must be a list")
                self.assertEqual("claude-mods", marketplace.name)

    def test_each_entry_must_be_an_object(self) -> None:
        entry = {"name": "alpha", "source": "./plugins/alpha"}
        self.set_marketplace(plugins=[entry, "beta"])
        self.expect("plugins[1] must be an object")

    def test_entry_name_must_be_present_and_kebab_case(self) -> None:
        self.set_entry(name=...)
        self.expect("plugins[0]: name is missing")
        for name in ("Alpha", "alpha_mod", 7):
            with self.subTest(name=name):
                self.set_entry(name=name)
                self.expect("is not kebab-case")

    def test_unsafe_entry_name_never_becomes_a_folder(self) -> None:
        self.set_entry(name="../escape")
        marketplace, problems = self.load()
        self.assertEqual({}, marketplace.entries)
        self.assert_found(problems, ERROR, MARKETPLACE, "is not kebab-case")

    def test_duplicate_entry_is_an_error(self) -> None:
        entry = {"name": "alpha", "source": "./plugins/alpha"}
        self.set_marketplace(plugins=[entry, dict(entry)])
        self.expect('"alpha" is listed twice')

    def test_source_must_be_the_mods_folder(self) -> None:
        for source in (
            "./plugins/other",
            "plugins/alpha",
            "./plugins/alpha/",
            {"source": "github", "repo": "someone/alpha"},
            ...,
        ):
            with self.subTest(source=source):
                self.set_entry(source=source)
                self.expect('it must be "./plugins/alpha"')

    def test_names_claude_code_reserves_are_errors(self) -> None:
        for name in (
            "claude",
            "anthropics",
            "claude-code",
            "claude-mods",
            "claude-band",
            "anthropic-tools",
            "anthropics-x",
            "cc-plugin-band",
            "official-claude",
            "band-anthropic-official",
        ):
            with self.subTest(name=name):
                make_mod(self.root, name)
                self.set_entry(name=name, source=f"./plugins/{name}")
                self.expect("Claude Code reserves this plugin name")

    def test_a_brand_word_elsewhere_in_a_name_is_a_warning(self) -> None:
        make_mod(self.root, "my-claude-band")
        self.set_entry(name="my-claude-band", source="./plugins/my-claude-band")
        problems = self.load()[1]
        self.assert_found(problems, WARNING, MARKETPLACE, "as a word")
        self.assertNotIn(ERROR, [p.level for p in problems if "alpha" not in str(p)])

    def test_names_that_only_contain_a_brand_are_fine(self) -> None:
        for name in ("claudette", "anthropology", "official-band", "x-claudes"):
            with self.subTest(name=name):
                make_mod(self.root, name)
                self.set_entry(name=name, source=f"./plugins/{name}")
                problems = self.load()[1]
                self.assertEqual([], [str(p) for p in problems if name in p.message])

    def test_entry_with_a_version_is_an_error(self) -> None:
        self.set_entry(version="0.1.0")
        self.expect("version lives in plugin.json only")

    def test_entry_without_a_folder_is_an_error(self) -> None:
        make_marketplace(self.root, ["alpha", "ghost"])
        self.expect('entry "ghost": plugins/ghost is missing')

    def test_description_must_match_plugin_json(self) -> None:
        self.set_entry(description="The alpha mod")
        self.assert_clean(self.load()[1])
        self.set_entry(description="Something else")
        self.expect("description differs")

    def test_folder_no_entry_lists_is_an_error(self) -> None:
        make_mod(self.root, "stray")
        self.expect("no marketplace entry lists this folder", where="plugins/stray")
        self.expect('add { "name": "stray"', where="plugins/stray")

    def test_a_folder_that_is_not_kebab_case_is_told_to_rename(self) -> None:
        for folder, suggestion in (
            ("My_Mod", "my-mod"),
            ("usageBand", "usage-band"),
            ("Big Band", "big-band"),
            ("___", "my-mod"),
        ):
            with self.subTest(folder=folder):
                make_mod(self.root, folder)
                problems = [p for p in self.load()[1] if folder in p.where]
                message = (
                    "folder name is not kebab-case: rename the folder and "
                    f'plugin.json\'s name, such as "{suggestion}"'
                )
                self.assertEqual(
                    [f"error: plugins/{folder}: {message}"], [str(p) for p in problems]
                )
                shutil.rmtree(self.root / "plugins" / folder)

    def test_a_listed_name_that_is_not_kebab_case_is_the_entrys_error(self) -> None:
        make_mod(self.root, "My_Mod")
        entry = {"name": "My_Mod", "source": "./plugins/My_Mod"}
        alpha = {"name": "alpha", "source": "./plugins/alpha"}
        self.set_marketplace(plugins=[alpha, entry])
        problems = self.load()[1]
        self.assertEqual(
            [f'error: {MARKETPLACE}: plugins[1]: name "My_Mod" is not kebab-case'],
            [str(p) for p in problems],
        )
        self.assertEqual("My_Mod", problems[0].mod)

    def test_each_entry_error_belongs_to_its_mod(self) -> None:
        self.set_entry(source="./plugins/alpha/", version="0.1.0")
        problems = self.load()[1]
        self.assertEqual(2, len(problems))
        self.assertEqual(["alpha", "alpha"], [p.mod for p in problems])

    def test_dot_folders_and_loose_files_are_not_mods(self) -> None:
        # Outside git: every folder counts, so this is the fallback's own rule.
        write(self.root, "plugins/.cache/state.json", "{}")
        write(self.root, "plugins/README.md", "# Mods\n")
        self.assert_clean(self.load()[1])


# --- mod_problems ------------------------------------------------------------


class ModProblemsTest(TempDirTest):
    MANIFEST = manifest("alpha")
    HOOKS = "plugins/alpha/hooks/hooks.json"
    CHANGELOG = "plugins/alpha/CHANGELOG.md"

    def setUp(self) -> None:
        super().setUp()
        make_mod(self.root, "alpha")

    def problems(self) -> list[Any]:
        return check_mods.mod_problems(self.root, "alpha")

    def expect(self, where: str, fragment: str, level: str = ERROR) -> None:
        """Assert the mod now draws a problem at `where` saying `fragment`."""
        self.assert_found(self.problems(), level, where, fragment)

    def set_manifest(self, **fields: object) -> None:
        data = read_json(self.root, self.MANIFEST)
        data.update(fields)
        write_json(self.root, self.MANIFEST, without_ellipsis(data))

    def set_changelog(self, text: str) -> None:
        write(self.root, self.CHANGELOG, text)

    def test_a_valid_mod_has_no_problems(self) -> None:
        self.assert_clean(self.problems())

    def test_missing_folder_is_one_error(self) -> None:
        problems = check_mods.mod_problems(self.root, "ghost")
        self.assertEqual(1, len(problems))
        self.assert_found(problems, ERROR, "plugins/ghost", "no such folder")

    def test_manifest_missing_or_unreadable_is_an_error(self) -> None:
        for content, fragment in (
            ("{ nope", "not valid JSON"),
            ("[]", "must be a JSON object"),
            (None, "missing"),
        ):
            with self.subTest(fragment=fragment):
                if content is None:
                    (self.root / self.MANIFEST).unlink()
                else:
                    write(self.root, self.MANIFEST, content)
                self.expect(self.MANIFEST, fragment)

    def test_name_must_be_the_folders(self) -> None:
        self.set_manifest(name="other")
        self.expect(self.MANIFEST, 'it must be the folder\'s name, "alpha"')
        self.set_manifest(name=...)
        self.expect(self.MANIFEST, "name is missing")

    def test_version_must_be_x_y_z(self) -> None:
        for version in (
            "v0.1.0",
            "0.1",
            "0.1.0-beta.1",
            "0.1.0.0",
            " 0.1.0",
            "0.1.01",
            "01.1.0",
            1,
            ...,
        ):
            with self.subTest(version=version):
                self.set_manifest(version=version)
                problems = self.problems()
                self.assert_found(problems, ERROR, self.MANIFEST, "version")
                self.assertEqual([self.MANIFEST], [p.where for p in problems])

    def test_a_leading_zero_is_named_in_the_error(self) -> None:
        self.set_manifest(version="0.1.01")
        fragment = 'version "0.1.01" is not X.Y.Z: three whole numbers without leading'
        self.expect(self.MANIFEST, fragment)

    def test_zeros_that_do_not_lead_are_fine(self) -> None:
        self.set_manifest(version="10.0.10")
        self.set_changelog("# alpha\n\n## 10.0.10 — 2026-11-02\n\n- x\n")
        self.assert_clean(self.problems())

    def test_description_must_be_a_non_empty_string(self) -> None:
        for description in ("", "   ", 5, ...):
            with self.subTest(description=description):
                self.set_manifest(description=description)
                self.expect(self.MANIFEST, "description is missing or empty")

    def test_author_needs_a_name(self) -> None:
        for author in ("Peter", {}, {"name": ""}, {"name": "  "}, {"name": 5}, ...):
            with self.subTest(author=author):
                self.set_manifest(author=author)
                self.expect(
                    self.MANIFEST, 'author must be an object with a non-empty "name"'
                )

    def test_types_naming_a_file_in_the_mod_passes(self) -> None:
        write(self.root, "plugins/alpha/types/index.d.ts", "export {}\n")
        self.set_manifest(types="./types/index.d.ts")
        self.assert_clean(self.problems())

    def test_types_must_name_a_file_in_the_mod(self) -> None:
        write(self.root, "plugins/outside.d.ts", "export {}\n")
        write(self.root, "plugins/alpha/types/index.d.ts", "export {}\n")
        for types, fragment in (
            (5, "types must be the path"),
            ("", "types must be the path"),
            ("types/index.d.ts", 'types must start with "./"'),
            (".\\types\\index.d.ts", 'types must start with "./"'),
            ("../outside.d.ts", 'types must start with "./"'),
            ("./types/missing.d.ts", "not a file in the mod"),
            ("./../outside.d.ts", "not a file in the mod"),
            ("./types", "not a file in the mod"),
        ):
            with self.subTest(types=types):
                self.set_manifest(types=types)
                self.expect(self.MANIFEST, fragment)

    def test_missing_hooks_json_is_only_a_warning(self) -> None:
        (self.root / self.HOOKS).unlink()
        problems = self.problems()
        self.assertEqual([WARNING], [p.level for p in problems])
        self.assert_found(problems, WARNING, self.HOOKS, "not a mod")

    def test_broken_hooks_json_is_an_error(self) -> None:
        write(self.root, "plugins/outside.tsx", "export {}\n")
        one = '"modules" must name exactly one hooks module'
        for content, fragment in (
            ("{ nope", "not valid JSON"),
            ("[]", "must be a JSON object"),
            ("{}", one),
            ('{"modules": []}', one),
            ('{"modules": "./register.tsx"}', one),
            ('{"modules": [3]}', one),
            ('{"modules": ["  "]}', one),
            ('{"modules": ["./register.tsx", 3]}', one),
            ('{"modules": ["./register.tsx", "./register.tsx"]}', one),
            ('{"modules": ["./missing.tsx"]}', "module ./missing.tsx is not a file"),
            ('{"modules": ["../../outside.tsx"]}', "is not a file in the mod"),
        ):
            with self.subTest(content=content):
                write(self.root, self.HOOKS, content)
                self.expect(self.HOOKS, fragment)

    def test_a_module_anywhere_in_the_mod_passes(self) -> None:
        write(self.root, "plugins/alpha/lib/register.tsx", "export {}\n")
        write_json(self.root, self.HOOKS, {"modules": ["../lib/register.tsx"]})
        self.assert_clean(self.problems())

    def test_missing_changelog_is_an_error(self) -> None:
        (self.root / self.CHANGELOG).unlink()
        self.expect(self.CHANGELOG, "missing")

    def test_changelog_without_the_current_version_is_an_error(self) -> None:
        self.set_changelog("# alpha\n\n## 0.0.9 — 2026-10-01\n\n- Earlier.\n")
        self.expect(self.CHANGELOG, "no CHANGELOG.md section for 0.1.0")

    def test_current_version_not_first_is_a_warning(self) -> None:
        older = "## 0.0.9 — 2026-10-01\n\n"
        self.set_changelog(f"# alpha\n\n{older}## 0.1.0 — 2026-11-02\n")
        problems = self.problems()
        self.assertEqual([WARNING], [p.level for p in problems])
        fragment = "the first section is 0.0.9, not 0.1.0"
        self.assert_found(problems, WARNING, self.CHANGELOG, fragment)

    def test_every_heading_form_counts(self) -> None:
        for heading in (
            "## 0.1.0 — 2026-11-02",
            "## 0.1.0",
            "## [0.1.0] - 2026-11-02",
            "## [0.1.0]",
            "## v0.1.0",
            "##\t0.1.0\t(2026-11-02)",
        ):
            with self.subTest(heading=heading):
                self.set_changelog(f"# alpha\n\n## Unreleased\n\n{heading}\r\n\n- x\n")
                self.assert_clean(self.problems())

    def test_a_heading_with_a_leading_zero_is_not_a_version(self) -> None:
        # Passed over like "## Unreleased": no "first section" warning about it.
        draft = "## 0.1.00 — draft\n\n"
        self.set_changelog(f"# alpha\n\n{draft}## 0.1.0 — 2026-11-02\n")
        self.assert_clean(self.problems())
        self.set_changelog(f"# alpha\n\n{draft}## 0.0.9 — 2026-10-01\n")
        self.expect(self.CHANGELOG, "no CHANGELOG.md section for 0.1.0")

    def test_near_miss_headings_do_not_count(self) -> None:
        for heading in (
            "## 0.1.00",
            "## 00.1.0",
            "## 0.01.0",
            "## 0.1.0-beta",
            "## 0.1.0.1",
            "### 0.1.0",
            "# 0.1.0",
            "##0.1.0",
            "## [0.1.0",
            "## version 0.1.0",
            "text ## 0.1.0",
        ):
            with self.subTest(heading=heading):
                self.set_changelog(f"# alpha\n\n{heading}\n")
                self.expect(self.CHANGELOG, "no CHANGELOG.md section")

    def test_byte_order_mark_in_plugin_json_is_a_warning(self) -> None:
        text = (self.root / self.MANIFEST).read_bytes()
        write(self.root, self.MANIFEST, b"\xef\xbb\xbf" + text)
        problems = self.problems()
        self.assertEqual([WARNING], [p.level for p in problems])
        self.assert_found(problems, WARNING, self.MANIFEST, check_mods.BOM)

    def test_byte_order_mark_in_hooks_json_is_a_warning(self) -> None:
        text = (self.root / self.HOOKS).read_bytes()
        write(self.root, self.HOOKS, b"\xef\xbb\xbf" + text)
        self.expect(self.HOOKS, check_mods.BOM, level=WARNING)

    def test_an_invalid_version_is_not_looked_for_in_the_changelog(self) -> None:
        self.set_manifest(version="one")
        self.assertEqual([self.MANIFEST], [p.where for p in self.problems()])


# --- readme_problems ---------------------------------------------------------


class ReadmeTest(TempDirTest):
    def problems(self, mods: Iterable[str] = ("alpha", "beta")) -> list[Any]:
        return check_mods.readme_problems(self.root, "claude-mods", mods)

    def expect(self, fragment: str, problems: list[Any] | None = None) -> None:
        found = self.problems() if problems is None else problems
        self.assert_found(found, ERROR, "README.md", fragment)

    def test_missing_readme_is_an_error(self) -> None:
        self.expect("missing")

    def test_every_mod_mentioned_passes(self) -> None:
        make_readme(self.root, ["alpha", "beta"])
        self.assert_clean(self.problems())

    def test_mod_it_does_not_mention_is_an_error(self) -> None:
        make_readme(self.root, ["alpha"])
        problems = self.problems()
        self.assertEqual(1, len(problems))
        self.expect("does not mention beta@claude-mods", problems)

    def test_mention_of_a_mod_that_does_not_exist_is_an_error(self) -> None:
        make_readme(self.root, ["alpha", "beta", "ghost"])
        self.expect("names ghost@claude-mods, but no mod is called ghost")

    def test_mentions_inside_prose_and_markup_count(self) -> None:
        text = "Install alpha@claude-mods.\n| `beta` | (beta@claude-mods) |\n"
        write(self.root, "README.md", text)
        self.assert_clean(self.problems())

    def test_other_marketplaces_are_not_mentions(self) -> None:
        text = "alpha@claude-mods-extra, beta@other-mods and ghost@claude-modsx\n"
        write(self.root, "README.md", text)
        problems = self.problems()
        self.expect("does not mention alpha@claude-mods", problems)
        self.expect("does not mention beta@claude-mods", problems)
        self.assertFalse(any("names" in p.message for p in problems))

    def test_without_a_marketplace_name_only_the_file_is_checked(self) -> None:
        write(self.root, "README.md", "# Nothing about mods\n")
        self.assert_clean(check_mods.readme_problems(self.root, "", ["alpha"]))

    def test_a_mention_inside_an_html_comment_does_not_count(self) -> None:
        for text in (
            "beta@claude-mods\n<!-- retired from the table: alpha@claude-mods -->\n",
            "beta@claude-mods\n<!--\n| alpha | alpha@claude-mods |\n-->\n",
            "beta@claude-mods\n<!-- never closed, so the rest is hidden\nalpha@claude-mods\n",
        ):
            with self.subTest(text=text):
                write(self.root, "README.md", text)
                problems = self.problems()
                self.assertEqual(1, len(problems))
                self.expect("does not mention alpha@claude-mods", problems)

    def test_a_mention_beside_a_comment_still_counts(self) -> None:
        text = "<!-- the table -->\nalpha@claude-mods <!-- x --> beta@claude-mods\n"
        write(self.root, "README.md", text)
        self.assert_clean(self.problems())

    def test_an_address_on_the_marketplaces_domain_is_not_a_mention(self) -> None:
        make_readme(self.root, ["alpha", "beta"])
        append(
            self.root, "README.md", "Mail peter@claude-mods.dev or me@claude-mods.\n"
        )
        problems = self.problems()
        self.expect("names me@claude-mods, but no mod is called me", problems)
        self.assertEqual(1, len(problems))

    def test_a_name_that_is_not_kebab_case_is_not_looked_for(self) -> None:
        make_readme(self.root, ["alpha"])
        self.assert_clean(self.problems(["alpha", "My_Mod"]))

    def test_each_missing_mention_belongs_to_its_mod(self) -> None:
        make_readme(self.root, ["alpha"])
        self.assertEqual(["beta"], [p.mod for p in self.problems()])


# --- generated_problems ------------------------------------------------------


class GeneratedTest(GitRepoTest):
    TYPES = "plugins/alpha/.claude-plugin/types"

    def write_types(self) -> None:
        write(self.root, f"{self.TYPES}/index.d.ts", "export {}\n")
        write(self.root, f"{self.TYPES}/state.d.ts", "export {}\n")

    def test_ignored_generated_types_pass(self) -> None:
        self.write_types()
        self.assert_clean(check_mods.generated_problems(self.root))

    def test_tracked_generated_types_are_an_error(self) -> None:
        self.write_types()
        git(self.root, "add", "-f", self.TYPES)
        self.commit()
        problems = check_mods.generated_problems(self.root)
        self.assertEqual(1, len(problems))
        fragment = "generated types are tracked (2 files)"
        self.assert_found(problems, ERROR, self.TYPES, fragment)
        fragment = (
            f"when it loads the mod with --plugin-dir: git rm --cached -r {self.TYPES}"
        )
        self.assert_found(problems, ERROR, self.TYPES, fragment)

    def test_staged_generated_types_are_an_error(self) -> None:
        self.write_types()
        git(self.root, "add", "-f", self.TYPES)
        problems = check_mods.generated_problems(self.root)
        self.assert_found(problems, ERROR, self.TYPES, "generated types are tracked")

    def test_untracking_them_clears_the_error(self) -> None:
        self.write_types()
        git(self.root, "add", "-f", self.TYPES)
        self.commit()
        git(self.root, "rm", "-r", "-q", "--cached", self.TYPES)
        self.assert_clean(check_mods.generated_problems(self.root))

    def test_tracked_types_are_reported_whatever_is_selected(self) -> None:
        self.write_types()
        git(self.root, "add", "-f", self.TYPES)
        code, out = self.check("--changed", "--mod", "beta")
        self.assertEqual(1, code)
        self.assertIn(f"error: {self.TYPES}: generated types are tracked", out)


class GeneratedOutsideGitTest(TempDirTest):
    def test_outside_git_there_is_nothing_to_report(self) -> None:
        write(self.root, "plugins/alpha/.claude-plugin/types/index.d.ts", "")
        self.assert_clean(check_mods.generated_problems(self.root))


# --- base_commit and changed_files -------------------------------------------


class BaseCommitTest(GitRepoTest):
    def test_main_is_the_default_base(self) -> None:
        edit(self.root, "alpha")
        self.commit()
        self.assertEqual(self.first, check_mods.base_commit(self.root, None))

    def test_the_base_is_the_merge_base_not_mains_tip(self) -> None:
        git(self.root, "checkout", "-q", "main")
        write(self.root, "notes.txt", "main moved on\n")
        self.commit("main moves")
        git(self.root, "checkout", "-q", "feat/work")
        self.assertEqual(self.first, check_mods.base_commit(self.root, None))

    def test_origin_main_wins_over_main(self) -> None:
        edit(self.root, "alpha")
        tip = self.commit()
        git(self.root, "update-ref", "refs/remotes/origin/main", self.first)
        git(self.root, "branch", "-f", "main", tip)
        self.assertEqual(self.first, check_mods.base_commit(self.root, None))

    def test_an_explicit_ref_wins(self) -> None:
        git(self.root, "tag", "first")
        edit(self.root, "alpha")
        tip = self.commit()
        self.assertEqual(tip, check_mods.base_commit(self.root, "HEAD"))
        self.assertEqual(self.first, check_mods.base_commit(self.root, "first"))

    def test_a_ref_that_does_not_resolve_gives_no_base(self) -> None:
        self.assertIsNone(check_mods.base_commit(self.root, "no-such-branch"))

    def test_no_main_gives_no_base(self) -> None:
        git(self.root, "branch", "-m", "main", "trunk")
        self.assertIsNone(check_mods.base_commit(self.root, None))

    def test_an_unrelated_origin_main_is_not_passed_over_for_main(self) -> None:
        git(self.root, "checkout", "-q", "--orphan", "unrelated")
        unrelated = self.commit("history of its own")
        git(self.root, "checkout", "-q", "feat/work")
        git(self.root, "update-ref", "refs/remotes/origin/main", unrelated)
        self.assertIsNone(check_mods.base_commit(self.root, None))
        self.assertEqual(self.first, check_mods.base_commit(self.root, "main"))
        code, out = self.check()
        self.assertEqual(1, code)
        self.assertIn("error: .: origin/main shares no history with HEAD here", out)
        self.assertNotIn("no main to compare against", out)
        self.assertEqual(0, self.check("--base", "main")[0])


class UnbornBaseTest(TempDirTest):
    def test_a_repo_without_commits_has_no_base(self) -> None:
        git(self.root, "init", "-q")
        self.assertIsNone(check_mods.base_commit(self.root, None))


class ChangedFilesTest(GitRepoTest):
    def changed(self) -> set[str]:
        return check_mods.changed_files(self.root, self.base())

    def test_an_untouched_branch_changes_nothing(self) -> None:
        self.assertEqual(set(), self.changed())

    def test_every_kind_of_change_counts(self) -> None:
        edit(self.root, "alpha")
        self.commit()  # committed on the branch
        write(self.root, "plugins/beta/hooks/register.tsx", "// staged\n")
        git(self.root, "add", "plugins/beta/hooks/register.tsx")
        write(self.root, "README.md", "# Unstaged\n")
        write(self.root, "plugins/beta/hooks/new.tsx", "// untracked\n")
        (self.root / "plugins/beta/CHANGELOG.md").unlink()  # deleted
        write(self.root, "plugins/beta/.claude-plugin/types/x.d.ts", "// ignored\n")
        self.assertEqual(
            {
                "plugins/alpha/hooks/register.tsx",
                "plugins/beta/hooks/register.tsx",
                "README.md",
                "plugins/beta/hooks/new.tsx",
                "plugins/beta/CHANGELOG.md",
            },
            self.changed(),
        )

    def test_a_rename_lists_both_paths(self) -> None:
        old, new = "plugins/alpha/hooks/register.tsx", "plugins/alpha/hooks/main.tsx"
        git(self.root, "mv", old, new)
        self.assertEqual({old, new}, self.changed())

    def test_a_staged_change_counts_when_the_working_tree_undoes_it(self) -> None:
        path = "plugins/alpha/hooks/register.tsx"
        original = (self.root / path).read_bytes()
        write(self.root, path, "// staged\n")
        git(self.root, "add", path)
        write(self.root, path, original)
        self.assertEqual({path}, self.changed())

    def test_non_ascii_paths_come_back_unquoted(self) -> None:
        untracked = "plugins/alpha/hooks/ünïcode.tsx"
        staged = "plugins/alpha/hooks/日本.tsx"
        write(self.root, untracked, "// new\n")
        write(self.root, staged, "// new\n")
        git(self.root, "add", staged)
        self.assertEqual({untracked, staged}, self.changed())


# --- the version gate --------------------------------------------------------


class VersionGateTest(GitRepoTest):
    ALPHA = manifest("alpha")

    def test_an_untouched_branch_passes(self) -> None:
        code, out = self.check()
        self.assertEqual(0, code)
        self.assertIn("ok: alpha 0.1.0 (main: 0.1.0)\n", out)
        self.assertIn("ok: beta 1.2.3 (main: 1.2.3)\n", out)
        self.assertTrue(out.endswith("0 error(s), 0 warning(s)\n"))

    def test_a_change_without_a_bump_is_an_error(self) -> None:
        edit(self.root, "alpha")
        problems = self.versions()
        self.assertEqual(1, len(problems))
        fragment = "changed since main but version is still 0.1.0"
        self.assert_found(problems, ERROR, self.ALPHA, fragment)
        code, out = self.check()
        self.assertEqual(1, code)
        self.assertNotIn("ok: alpha", out)
        self.assertIn("ok: beta 1.2.3 (main: 1.2.3)", out)

    def test_a_committed_change_without_a_bump_is_an_error(self) -> None:
        edit(self.root, "alpha")
        self.commit()
        self.assert_found(self.versions(), ERROR, self.ALPHA, "version is still 0.1.0")

    def test_a_staged_change_without_a_bump_is_an_error(self) -> None:
        edit(self.root, "alpha")
        git(self.root, "add", "-A")
        self.assert_found(self.versions(), ERROR, self.ALPHA, "version is still 0.1.0")

    def test_an_untracked_file_without_a_bump_is_an_error(self) -> None:
        write(self.root, "plugins/alpha/hooks/extra.tsx", "export {}\n")
        self.assert_found(self.versions(), ERROR, self.ALPHA, "version is still 0.1.0")

    def test_a_deleted_file_without_a_bump_is_an_error(self) -> None:
        git(self.root, "rm", "-q", "plugins/alpha/hooks/register.tsx")
        self.assert_found(self.versions(), ERROR, self.ALPHA, "version is still 0.1.0")
        self.commit("delete a shipped file")
        self.assert_found(self.versions(), ERROR, self.ALPHA, "version is still 0.1.0")

    def test_each_bump_size_passes(self) -> None:
        edit(self.root, "alpha")
        for version in ("0.1.1", "0.2.0", "1.0.0"):
            with self.subTest(version=version):
                bump(self.root, "alpha", version)
                self.assert_clean(self.versions())
                self.assert_clean(check_mods.mod_problems(self.root, "alpha"))

    def test_a_bump_alone_passes(self) -> None:
        bump(self.root, "alpha", "0.1.1")
        self.assert_clean(self.versions())

    def test_a_lower_version_is_an_error(self) -> None:
        bump(self.root, "beta", "1.2.2")
        fragment = "version 1.2.2 is lower than main's 1.2.3"
        self.assert_found(self.versions(), ERROR, manifest("beta"), fragment)

    def test_a_jump_is_a_warning(self) -> None:
        edit(self.root, "alpha")
        bump(self.root, "alpha", "0.3.0")
        problems = self.versions()
        self.assertEqual([WARNING], [p.level for p in problems])
        fragment = "jumps from 0.1.0 to 0.3.0; expected one of 0.1.1, 0.2.0, 1.0.0"
        self.assert_found(problems, WARNING, self.ALPHA, fragment)
        code, out = self.check()
        self.assertEqual(0, code)
        self.assertIn("ok: alpha 0.3.0 (main: 0.1.0)", out)

    def test_files_that_do_not_ship_need_no_bump(self) -> None:
        append(self.root, "plugins/alpha/CHANGELOG.md", "- More.\n")
        write(self.root, "plugins/alpha/README.md", "# alpha\n")
        write(self.root, "plugins/alpha/tsconfig.json", "{}\n")
        write(self.root, "plugins/alpha/hooks/register.test.ts", "// test\n")
        write(self.root, "plugins/alpha/hooks/ui/view.test.tsx", "// test\n")
        write(self.root, "plugins/alpha/.claude-plugin/types/x.d.ts", "// generated\n")
        self.assert_clean(self.versions())
        self.assertEqual(0, self.check()[0])

    def test_a_new_mod_has_nothing_to_compare_against(self) -> None:
        make_mod(self.root, "gamma", "0.4.0")
        make_marketplace(self.root, ["alpha", "beta", "gamma"])
        make_readme(self.root, ["alpha", "beta", "gamma"])
        self.assert_clean(self.versions("gamma"))
        code, out = self.check()
        self.assertEqual(0, code)
        self.assertIn("ok: gamma 0.4.0 (main: new)", out)

    def test_a_removed_mod_is_a_warning(self) -> None:
        shutil.rmtree(self.root / "plugins/beta")
        make_marketplace(self.root, ["alpha"])
        make_readme(self.root, ["alpha"])
        code, out = self.check()
        self.assertEqual(0, code)
        self.assertEqual(
            [
                (
                    "warning: plugins/beta: removed: machines that installed "
                    "beta@claude-mods stop loading it once their copy of the "
                    'marketplace refreshes, and report "Plugin beta not found in '
                    'marketplace claude-mods" until they run claude plugin '
                    "uninstall beta@claude-mods"
                )
            ],
            output_lines(out, "warning"),
        )

    def test_a_removed_mods_leftover_generated_types_are_not_a_mod(self) -> None:
        shutil.rmtree(self.root / "plugins/beta")
        write(self.root, "plugins/beta/.claude-plugin/types/x.d.ts", "// left\n")
        make_marketplace(self.root, ["alpha"])
        make_readme(self.root, ["alpha"])
        code, out = self.check()
        self.assertEqual(0, code)
        self.assertEqual([], output_lines(out, "error"))
        self.assertIn("warning: plugins/beta: removed:", out)

    def test_an_unreadable_version_on_main_is_a_warning(self) -> None:
        git(self.root, "checkout", "-q", "main")
        data = read_json(self.root, self.ALPHA)
        data["version"] = "one"
        write_json(self.root, self.ALPHA, data)
        self.commit("main gets a bad version")
        git(self.root, "checkout", "-q", "-b", "feat/repair")
        bump(self.root, "alpha", "0.1.0")
        fragment = 'main\'s version "one" is not X.Y.Z: version not compared'
        self.assert_found(self.versions(), WARNING, self.ALPHA, fragment)
        code, out = self.check()
        self.assertEqual(0, code)
        self.assertIn("ok: alpha 0.1.0 (main: unreadable)", out)

    def test_only_the_mods_asked_about_are_compared(self) -> None:
        edit(self.root, "alpha")
        self.assert_clean(self.versions("beta"))

    def test_dot_folders_and_loose_files_are_not_mods(self) -> None:
        # In git, where every real run is: the listing, not the fallback, decides.
        write(self.root, "plugins/.cache/state.json", "{}")
        write(self.root, "plugins/README.md", "# Mods\n")
        code, out = self.check()
        self.assertEqual(0, code)
        self.assertEqual([], output_lines(out, "error"))
        self.assertTrue(out.endswith("0 error(s), 0 warning(s)\n"))
        code, out = self.check("--changed")
        self.assertEqual("0 error(s), 0 warning(s)\n", out)


class BomOnMainTest(GitRepoTest):
    """main's alpha plugin.json was saved with a byte-order mark."""

    def prepare(self) -> None:
        text = (self.root / manifest("alpha")).read_bytes()
        write(self.root, manifest("alpha"), b"\xef\xbb\xbf" + text)

    def test_mains_version_is_read_through_the_mark(self) -> None:
        self.assert_clean(self.versions())
        edit(self.root, "alpha")
        fragment = "changed since main but version is still 0.1.0"
        self.assert_found(self.versions(), ERROR, manifest("alpha"), fragment)


class RenameTest(GitRepoTest):
    def rename(self, name: str, mods: Iterable[str] = ("alpha", "beta")) -> None:
        make_marketplace(self.root, mods, name=name)
        make_readme(self.root, mods, marketplace=name)

    def test_a_renamed_marketplace_is_a_warning(self) -> None:
        self.rename("peter-mods")
        code, out = self.check()
        self.assertEqual(0, code)
        message = (
            "renamed from claude-mods to peter-mods: machines that added "
            "claude-mods keep it under that name, and the README's commands no "
            "longer match them"
        )
        self.assertEqual(
            [f"warning: {MARKETPLACE}: {message}"], output_lines(out, "warning")
        )

    def test_a_removed_mod_is_named_as_machines_installed_it(self) -> None:
        shutil.rmtree(self.root / "plugins/beta")
        self.rename("peter-mods", ["alpha"])
        out = self.check()[1]
        self.assertIn(
            "warning: plugins/beta: removed: machines that installed beta@claude-mods",
            out,
        )
        self.assertIn('"Plugin beta not found in marketplace claude-mods"', out)
        self.assertNotIn("beta@peter-mods", out)


class NewMarketplaceTest(GitRepoTest):
    """main predates the marketplace file, as main before this repo's first mod."""

    def prepare(self) -> None:
        (self.root / MARKETPLACE).unlink()

    def test_a_file_main_never_had_is_an_answer_not_a_git_failure(self) -> None:
        make_marketplace(self.root, ["alpha", "beta"])
        code, out = self.check()
        self.assertEqual(0, code)
        self.assertEqual("0 error(s), 0 warning(s)", out.splitlines()[-1])
        self.assertIn("ok: alpha 0.1.0 (main: 0.1.0)", out)


class NoMainTest(GitRepoTest):
    def setUp(self) -> None:
        super().setUp()
        git(self.root, "branch", "-m", "main", "trunk")

    def test_version_checks_are_skipped_with_a_warning(self) -> None:
        edit(self.root, "alpha")
        code, out = self.check()
        self.assertEqual(0, code)
        self.assertEqual(
            ["warning: .: no main to compare against: version checks skipped"],
            output_lines(out, "warning"),
        )
        self.assertIn("ok: alpha 0.1.0 (main: not compared)", out)

    def test_changed_checks_every_mod(self) -> None:
        code, out = self.check("--changed")
        self.assertEqual(0, code)
        self.assertIn("ok: alpha", out)
        self.assertIn("ok: beta", out)


class BaseFlagTest(GitRepoTest):
    def test_base_moves_the_comparison(self) -> None:
        edit(self.root, "alpha")
        bump(self.root, "alpha", "0.2.0")
        self.commit()
        edit(self.root, "alpha")
        self.assertEqual(0, self.check()[0])
        code, out = self.check("--base", "HEAD")
        self.assertEqual(1, code)
        line = "changed since main but version is still 0.2.0"
        self.assertIn(f"error: {manifest('alpha')}: {line}", out)

    def test_a_base_that_does_not_resolve_is_an_error(self) -> None:
        code, out = self.check("--base", "no-such-ref")
        self.assertEqual(1, code)
        self.assertIn("error: .: --base no-such-ref names no commit", out)


class ShallowCloneTest(GitRepoTest):
    """A clone with too little history: origin/main is there, its past is not."""

    SHALLOW = (
        "error: .: origin/main shares no history with HEAD here (a shallow clone? "
        "run git fetch --unshallow): versions not compared"
    )

    def setUp(self) -> None:
        super().setUp()
        edit(self.root, "alpha")
        self.commit("an unbumped change")
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.clone = Path(temp.name).resolve() / "clone"
        # Over file://, since git ignores --depth when it clones a local path.
        source = self.root.as_uri()
        git(
            self.root,
            "clone",
            "-q",
            "-c",
            "core.autocrlf=false",
            "--depth",
            "1",
            "--no-single-branch",
            "--branch",
            "feat/work",
            source,
            str(self.clone),
        )

    def check_clone(self, *args: str) -> tuple[int, str]:
        return self.run_main("--root", str(self.clone), "--no-cli", *args)

    def test_a_shallow_clone_is_an_error_not_a_skipped_check(self) -> None:
        for args in ((), ("--changed",), ("--base", "origin/main")):
            with self.subTest(args=args):
                code, out = self.check_clone(*args)
                self.assertEqual(1, code)
                self.assertIn(self.SHALLOW, output_lines(out, "error"))
                self.assertNotIn("no main to compare against", out)

    def test_the_full_history_brings_the_comparison_back(self) -> None:
        git(self.clone, "fetch", "-q", "--unshallow")
        code, out = self.check_clone()
        self.assertEqual(1, code)
        self.assertNotIn("shares no history", out)
        line = "changed since main but version is still 0.1.0"
        self.assertIn(f"error: {manifest('alpha')}: {line}", out)


class GitFailureTest(GitRepoTest):
    """A git command that fails is an error, never an empty answer.

    The branch holds an unbumped change to alpha, so an empty answer would
    pass it.
    """

    def setUp(self) -> None:
        super().setUp()
        edit(self.root, "alpha")
        self.commit("an unbumped change")

    def assert_git_error(self, out: str, command: str, reason: str) -> None:
        """Assert that an error line says `git <command>...` failed for `reason`."""
        prefix = f"error: .: git {command}"
        found = [
            line
            for line in output_lines(out, "error")
            if line.startswith(prefix) and f" failed: {reason}" in line
        ]
        if not found:
            self.fail(f"no line starting {prefix!r} failing {reason!r}:\n{out}")

    def test_a_broken_git_diff_is_an_error_until_git_is_mended(self) -> None:
        git(self.root, "config", "diff.algorithm", "bogus")
        for args in ((), ("--changed",)):
            with self.subTest(args=args):
                code, out = self.check(*args)
                self.assertEqual(1, code)
                self.assert_git_error(
                    out,
                    "diff --name-only --no-renames --relative -z",
                    "fatal: bad config variable 'diff.algorithm'",
                )
                self.assertEqual(1, len(output_lines(out, "error")), out)
                self.assertIn("ok: alpha 0.1.0 (main: not compared)", out)
        git(self.root, "config", "--unset", "diff.algorithm")
        code, out = self.check()
        self.assertEqual(1, code)
        self.assertIn("changed since main but version is still 0.1.0", out)
        self.assertNotIn("failed", out)

    def test_each_command_the_checks_need_is_an_error_when_it_fails(self) -> None:
        for prefix, command in (
            (("diff",), "diff --name-only"),
            (("ls-files",), "ls-files -z"),
            (("ls-tree",), "ls-tree -r -z --name-only"),
            (("cat-file",), "cat-file -p"),
            (("merge-base",), "merge-base HEAD main"),
            (("rev-parse", "--verify"), "rev-parse --verify --quiet origin/main"),
        ):
            with self.subTest(command=command), git_failing(*prefix):
                code, out = self.check()
                self.assertEqual(1, code)
                self.assert_git_error(out, command, "fatal: simulated")
                self.assertNotIn("(main: 0.1.0)", out)

    def test_a_git_that_runs_past_its_timeout_is_an_error(self) -> None:
        timeout = subprocess.TimeoutExpired(["git"], check_mods.GIT_TIMEOUT_SECONDS)
        for prefix, command in (
            (("ls-tree",), "ls-tree -r -z --name-only"),
            (("rev-parse", "--is-inside-work-tree"), "rev-parse --is-inside-work-tree"),
        ):
            with self.subTest(command=command), git_failing(*prefix, error=timeout):
                code, out = self.check()
                self.assertEqual(1, code)
                self.assert_git_error(out, command, "timed out after 60 s")
                self.assertNotIn("not a git repository", out)

    def test_a_probe_that_answers_no_is_not_a_failure(self) -> None:
        # Exit 1 from rev-parse --verify is "no such commit", for both candidates.
        with git_failing("rev-parse", "--verify", code=1):
            code, out = self.check()
        self.assertEqual(0, code)
        self.assertEqual(
            ["warning: .: no main to compare against: version checks skipped"],
            output_lines(out, "warning"),
        )
        self.assertNotIn("failed", out)

    def test_no_git_at_all_is_still_reported_as_no_repository(self) -> None:
        missing = FileNotFoundError(2, "No such file or directory")
        with git_failing(error=missing):
            code, out = self.check()
        self.assertEqual(1, code)
        message = (
            "not a git repository, or git is not installed: the version, "
            "--changed and generated-types checks did not run"
        )
        self.assertEqual([f"error: .: {message}"], output_lines(out, "error"))
        self.assertIn("ok: alpha 0.1.0 (main: not compared)", out)


class SubfolderRootTest(GitRepoTest):
    """The marketplace may sit below the top of its repository."""

    def setUp(self) -> None:
        super().setUp()
        self.top = self.root
        self.root = self.top / "market"
        write(self.top, "market/.gitignore", "plugins/*/.claude-plugin/types/\n")
        make_repo(self.root, {"gamma": "2.0.0"})
        git(self.top, "add", "-A")
        git(self.top, "commit", "-q", "--no-verify", "-m", "a marketplace below")
        git(self.top, "branch", "-f", "main", "HEAD")

    def test_paths_and_versions_are_read_relative_to_the_root(self) -> None:
        code, out = self.check()
        self.assertEqual(0, code)
        self.assertIn("ok: gamma 2.0.0 (main: 2.0.0)", out)
        edit(self.root, "gamma")
        code, out = self.check()
        self.assertEqual(1, code)
        line = "changed since main but version is still 2.0.0"
        self.assertIn(f"error: {manifest('gamma')}: {line}", out)


# --- selection ---------------------------------------------------------------


class SelectionTest(GitRepoTest):
    """beta's changelog lacks its current version on main already."""

    BETA = "plugins/beta/CHANGELOG.md"

    def prepare(self) -> None:
        write(self.root, self.BETA, "# beta\n\n## 1.0.0 — 2026-01-01\n")

    def test_every_mod_is_checked_by_default(self) -> None:
        code, out = self.check()
        self.assertEqual(1, code)
        self.assertIn(f"error: {self.BETA}: no CHANGELOG.md section for 1.2.3", out)
        self.assertIn("ok: alpha 0.1.0 (main: 0.1.0)", out)

    def test_changed_checks_only_changed_mods(self) -> None:
        edit(self.root, "alpha")
        bump(self.root, "alpha", "0.1.1")
        code, out = self.check("--changed")
        self.assertEqual(0, code)
        self.assertIn("ok: alpha 0.1.1 (main: 0.1.0)", out)
        self.assertNotIn("beta", out)

    def test_changed_counts_files_that_do_not_ship(self) -> None:
        append(self.root, "plugins/alpha/CHANGELOG.md", "- More.\n")
        code, out = self.check("--changed")
        self.assertEqual(0, code)
        self.assertIn("ok: alpha 0.1.0 (main: 0.1.0)", out)

    def test_changed_with_nothing_changed_checks_no_mod(self) -> None:
        code, out = self.check("--changed")
        self.assertEqual(0, code)
        self.assertEqual("0 error(s), 0 warning(s)\n", out)

    def test_mod_narrows_to_the_named_mods(self) -> None:
        code, out = self.check("--mod", "alpha")
        self.assertEqual(0, code)
        self.assertIn("ok: alpha", out)
        self.assertNotIn("beta", out)
        code, out = self.check("--mod", "beta")
        self.assertEqual(1, code)
        self.assertIn(f"error: {self.BETA}", out)
        self.assertNotIn("ok: alpha", out)
        code, out = self.check("--mod", "alpha", "--mod", "beta")
        self.assertEqual(1, code)
        self.assertIn("ok: alpha", out)
        self.assertIn(f"error: {self.BETA}", out)

    def test_mod_and_changed_narrow_together(self) -> None:
        edit(self.root, "alpha")
        bump(self.root, "alpha", "0.1.1")
        code, out = self.check("--changed", "--mod", "beta")
        self.assertEqual(0, code)
        self.assertEqual("0 error(s), 0 warning(s)\n", out)

    def test_whole_repo_checks_ignore_the_selection(self) -> None:
        make_readme(self.root, ["beta"])
        make_mod(self.root, "stray")
        code, out = self.check("--changed", "--mod", "beta")
        self.assertEqual(1, code)
        self.assertIn("error: README.md: does not mention alpha@claude-mods", out)
        self.assertIn("error: plugins/stray: no marketplace entry lists", out)

    def test_an_unknown_mod_is_an_error(self) -> None:
        code, out = self.check("--mod", "ghost")
        self.assertEqual(1, code)
        self.assertIn("error: plugins/ghost: no mod named ghost", out)

    def test_naming_a_removed_mod_reports_its_removal(self) -> None:
        shutil.rmtree(self.root / "plugins/alpha")
        make_marketplace(self.root, ["beta"])
        make_readme(self.root, ["beta"])
        code, out = self.check("--mod", "alpha")
        self.assertEqual(0, code)
        self.assertIn("warning: plugins/alpha: removed:", out)
        self.assertNotIn("no mod named", out)


# --- main --------------------------------------------------------------------


class MainTest(GitRepoTest):
    def test_errors_come_before_warnings_and_are_counted(self) -> None:
        text = (self.root / MARKETPLACE).read_bytes()
        write(self.root, MARKETPLACE, b"\xef\xbb\xbf" + text)
        make_readme(self.root, ["alpha"])
        code, out = self.check()
        self.assertEqual(1, code)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("error: README.md: does not mention"))
        self.assertTrue(lines[1].startswith(f"warning: {MARKETPLACE}: saved with"))
        self.assertEqual("1 error(s), 1 warning(s)", lines[-1])

    def test_an_ok_line_survives_a_warning_but_not_an_error(self) -> None:
        (self.root / "plugins/alpha/hooks/hooks.json").unlink()
        bump(self.root, "alpha", "0.1.1")
        write(self.root, "plugins/beta/hooks/hooks.json", "{ nope")
        bump(self.root, "beta", "1.2.4")
        code, out = self.check()
        self.assertEqual(1, code)
        self.assertIn("ok: alpha 0.1.1 (main: 0.1.0)", out)
        self.assertNotIn("ok: beta", out)

    def test_an_error_in_a_mods_entry_withholds_its_ok_line(self) -> None:
        data = read_json(self.root, MARKETPLACE)
        entry = {"name": "alpha", "source": "./plugins/alpha/", "version": "0.1.0"}
        data["plugins"][0] = entry
        write_json(self.root, MARKETPLACE, data)
        code, out = self.check()
        self.assertEqual(1, code)
        self.assertEqual(2, len(output_lines(out, "error")))
        self.assertNotIn("ok: alpha", out)
        self.assertIn("ok: beta 1.2.3 (main: 1.2.3)", out)

    def test_a_readme_that_leaves_a_mod_out_withholds_its_ok_line(self) -> None:
        make_readme(self.root, ["beta"])
        code, out = self.check()
        self.assertEqual(1, code)
        self.assertNotIn("ok: alpha", out)
        self.assertIn("ok: beta 1.2.3 (main: 1.2.3)", out)

    def test_a_folder_that_is_not_kebab_case_draws_one_error(self) -> None:
        make_mod(self.root, "My_Mod")
        code, out = self.check()
        self.assertEqual(1, code)
        message = (
            "folder name is not kebab-case: rename the folder and plugin.json's "
            'name, such as "my-mod"'
        )
        self.assertEqual(
            [f"error: plugins/My_Mod: {message}"], output_lines(out, "error")
        )
        self.assertNotIn("ok: My_Mod", out)

    def test_a_listed_name_that_is_not_kebab_case_asks_nothing_of_the_readme(
        self,
    ) -> None:
        make_mod(self.root, "My_Mod")
        make_marketplace(self.root, ["alpha", "beta", "My_Mod"])
        code, out = self.check()
        self.assertEqual(1, code)
        self.assertEqual(
            [f'error: {MARKETPLACE}: plugins[2]: name "My_Mod" is not kebab-case'],
            output_lines(out, "error"),
        )
        self.assertNotIn("ok: My_Mod", out)

    def test_no_validation_line_without_the_cli(self) -> None:
        self.assertNotIn("validated with", self.check()[1])

    def test_an_unknown_option_is_a_usage_error(self) -> None:
        self.assertEqual(2, self.run_main("--no-cli", "--bogus")[0])

    def test_a_root_that_does_not_exist_is_a_usage_error(self) -> None:
        missing = str(self.root / "nowhere")
        self.assertEqual(2, self.run_main("--root", missing, "--no-cli")[0])

    def test_help_exits_zero(self) -> None:
        code, out = self.run_main("--help")
        self.assertEqual(0, code)
        self.assertIn("--changed", out)

    def test_a_missing_marketplace_is_an_error_not_a_usage_error(self) -> None:
        (self.root / MARKETPLACE).unlink()
        code, out = self.check()
        self.assertEqual(1, code)
        self.assertIn(f"error: {MARKETPLACE}: missing", out)

    def test_the_default_root_is_the_folder_above_scripts(self) -> None:
        script = str(self.root / "scripts" / "check_mods.py")
        with mock.patch.object(check_mods, "__file__", script):
            code, out = self.run_main("--no-cli")
        self.assertEqual(0, code)
        self.assertIn("ok: alpha 0.1.0 (main: 0.1.0)", out)


class MainOutsideGitTest(TempDirTest):
    def test_a_folder_outside_git_is_an_error_but_still_checked(self) -> None:
        make_repo(self.root, {"alpha": "0.1.0"})
        code, out = self.check()
        self.assertEqual(1, code)
        self.assertIn("error: .: not a git repository", out)
        self.assertIn("ok: alpha 0.1.0 (main: not compared)", out)


# --- cli_problems (mocked: never the real CLI) -------------------------------


class FakeRun:
    """Stands in for subprocess.run: answers claude, lets git through."""

    def __init__(self, version_code: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.options: list[dict[str, Any]] = []
        self.answers: dict[tuple[str, ...], Any] = {}
        self.version_code = version_code

    def answer(self, *args: str, code: int = 1, output: str = "") -> None:
        process = subprocess.CompletedProcess(["claude", *args], code, output, "")
        self.answers[args] = process

    def raise_on(self, *args: str, error: BaseException) -> None:
        self.answers[args] = error

    def __call__(
        self, cmd: list[str], **options: Any
    ) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "git":
            return REAL_RUN(cmd, **options)
        self.calls.append(list(cmd))
        self.options.append(options)
        args = tuple(cmd[1:])
        if args == ("--version",):
            out = "2.1.287 (Claude Code)\n" if self.version_code == 0 else ""
            return subprocess.CompletedProcess(cmd, self.version_code, out, "")
        answer = self.answers.get(args)
        if isinstance(answer, BaseException):
            raise answer
        passed = subprocess.CompletedProcess(cmd, 0, "✔ Validation passed\n", "")
        return answer or passed

    def commands(self) -> list[list[str]]:
        return [call[1:] for call in self.calls]


CLAUDE = "/fake/bin/claude"


@contextlib.contextmanager
def fake_cli(
    fake: FakeRun, which: str | None = CLAUDE, flags: tuple[str, ...] = ()
) -> Iterator[mock.MagicMock]:
    """Put `fake` in place of the CLI, and `which` where shutil.which finds it.

    `flags` stands in for VALIDATE_FLAGS, empty by default so each test spells
    the exact commands it expects; the flags have tests of their own.
    """
    with (
        mock.patch.object(check_mods.shutil, "which", return_value=which) as found,
        mock.patch.object(check_mods.subprocess, "run", fake),
        mock.patch.object(check_mods, "VALIDATE_FLAGS", flags),
    ):
        yield found


class CliTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        make_repo(self.root, {"alpha": "0.1.0", "beta": "1.2.3"})

    def run_cli(
        self,
        fake: FakeRun,
        mods: Iterable[str] = ("alpha", "beta"),
        flags: tuple[str, ...] = (),
    ) -> tuple[list[Any], str | None]:
        with fake_cli(fake, flags=flags) as found:
            result = check_mods.cli_problems(self.root, mods)
        found.assert_called_once_with("claude")
        return result

    def test_validate_is_strict_by_default(self) -> None:
        self.assertEqual(("--strict",), check_mods.VALIDATE_FLAGS)

    def plugin_tests(self, fake: FakeRun) -> list[list[str]]:
        return [c for c in fake.commands() if c[:2] == ["plugin", "test"]]

    def test_a_missing_cli_is_an_error(self) -> None:
        fake = FakeRun()
        with fake_cli(fake, which=None):
            problems, version = check_mods.cli_problems(self.root, ["alpha"])
        self.assertIsNone(version)
        self.assertEqual(
            [
                (
                    "error: .: claude CLI not on PATH; nothing was validated (use "
                    "--no-cli to skip deliberately)"
                )
            ],
            [str(p) for p in problems],
        )
        self.assertEqual([], fake.calls)

    def test_validates_the_marketplace_and_each_mod(self) -> None:
        fake = FakeRun()
        problems, version = self.run_cli(fake)
        self.assert_clean(problems)
        self.assertEqual("2.1.287", version)
        self.assertEqual(
            [
                ["--version"],
                ["plugin", "validate", "."],
                ["plugin", "validate", "plugins/alpha"],
                ["plugin", "validate", "plugins/beta"],
            ],
            fake.commands(),
        )
        self.assertTrue(all(call[0] == CLAUDE for call in fake.calls))
        for options in fake.options:
            self.assertEqual(self.root, Path(options["cwd"]))
            self.assertEqual(180, options["timeout"])
            self.assertEqual("utf-8", options["encoding"])
            self.assertEqual("replace", options["errors"])

    def test_tests_run_only_for_mods_that_have_them(self) -> None:
        write(self.root, "plugins/beta/hooks/ui/view.test.tsx", "// test\n")
        fake = FakeRun()
        self.run_cli(fake)
        self.assertEqual([["plugin", "test", "plugins/beta"]], self.plugin_tests(fake))
        write(self.root, "plugins/alpha/hooks/clock.test.ts", "// test\n")
        fake = FakeRun()
        self.run_cli(fake)
        self.assertEqual(
            [["plugin", "test", "plugins/alpha"], ["plugin", "test", "plugins/beta"]],
            self.plugin_tests(fake),
        )

    def test_a_failed_validate_is_an_error_quoting_its_output_tail(self) -> None:
        fake = FakeRun()
        rows = "".join(f"row {i:02d}\n\n" for i in range(1, 30))
        # Colour on the last line, which the tail keeps, so stripping it shows.
        rows += "\x1b[31mrow 30\x1b[0m\n"
        fake.answer("plugin", "validate", "plugins/alpha", output=rows)
        problems, _ = self.run_cli(fake)
        self.assertEqual(1, len(problems))
        fragment = "claude plugin validate plugins/alpha failed (exit 1):"
        self.assert_found(problems, ERROR, "plugins/alpha", fragment)
        message = problems[0].message
        self.assertIn("\n    row 30", message)
        self.assertIn("\n    row 11", message)
        self.assertNotIn("row 10", message)
        self.assertNotIn("\x1b", message)

    def test_a_failed_marketplace_validate_points_at_the_marketplace(self) -> None:
        fake = FakeRun()
        fake.answer("plugin", "validate", ".", output="✘ Found 1 error\n")
        problems, _ = self.run_cli(fake)
        fragment = "failed (exit 1):\n    ✘ Found 1 error"
        self.assert_found(problems, ERROR, MARKETPLACE, fragment)

    def test_failing_tests_are_an_error(self) -> None:
        write(self.root, "plugins/alpha/hooks/clock.test.ts", "// test\n")
        fake = FakeRun()
        fake.answer("plugin", "test", "plugins/alpha", output="1 failed\n")
        problems, _ = self.run_cli(fake)
        fragment = "claude plugin test plugins/alpha failed (exit 1):\n    1 failed"
        self.assert_found(problems, ERROR, "plugins/alpha", fragment)

    def test_a_timeout_is_an_error(self) -> None:
        fake = FakeRun()
        timeout = subprocess.TimeoutExpired(["claude"], 180)
        fake.raise_on("plugin", "validate", "plugins/beta", error=timeout)
        problems, _ = self.run_cli(fake)
        self.assert_found(problems, ERROR, "plugins/beta", "timed out after 180 s")

    def test_a_cli_that_cannot_start_is_an_error(self) -> None:
        fake = FakeRun()
        fake.raise_on("plugin", "validate", ".", error=OSError("bad interpreter"))
        problems, _ = self.run_cli(fake)
        fragment = "could not run: bad interpreter"
        self.assert_found(problems, ERROR, MARKETPLACE, fragment)

    def test_validate_flags_reach_every_validate_and_only_validate(self) -> None:
        write(self.root, "plugins/alpha/hooks/clock.test.ts", "// test\n")
        fake = FakeRun()
        self.run_cli(fake, flags=("--strict",))
        self.assertEqual(1, len(self.plugin_tests(fake)))
        for command in fake.commands():
            with self.subTest(command=command):
                validates = command[:2] == ["plugin", "validate"]
                self.assertEqual(validates, "--strict" in command)

    def test_an_unreadable_version_still_validates(self) -> None:
        fake = FakeRun(version_code=1)
        problems, version = self.run_cli(fake)
        self.assert_clean(problems)
        self.assertEqual("unknown", version)
        self.assertIn(["plugin", "validate", "plugins/alpha"], fake.commands())

    def test_no_marketplace_file_means_no_marketplace_validate(self) -> None:
        (self.root / MARKETPLACE).unlink()
        fake = FakeRun()
        self.run_cli(fake)
        self.assertNotIn(["plugin", "validate", "."], fake.commands())

    def test_mods_without_a_folder_or_a_kebab_name_are_not_run(self) -> None:
        write(self.root, "plugins/Bad&Name/hooks/register.tsx", "// odd\n")
        fake = FakeRun()
        self.run_cli(fake, ["ghost", "Bad&Name"])
        self.assertEqual([["--version"], ["plugin", "validate", "."]], fake.commands())


class MainCliTest(GitRepoTest):
    def run_with(
        self, fake: FakeRun, *args: str, which: str | None = CLAUDE
    ) -> tuple[int, str]:
        with fake_cli(fake, which=which):
            return self.run_main("--root", str(self.root), *args)

    def test_main_reports_the_cli_version_it_validated_with(self) -> None:
        code, out = self.run_with(FakeRun())
        self.assertEqual(0, code)
        self.assertEqual(
            ["0 error(s), 0 warning(s)", "validated with Claude Code 2.1.287"],
            out.splitlines()[-2:],
        )

    def test_main_says_when_the_version_is_unknown(self) -> None:
        code, out = self.run_with(FakeRun(version_code=1))
        self.assertEqual(0, code)
        self.assertTrue(out.endswith("validated with Claude Code (version unknown)\n"))

    def test_main_fails_without_the_cli(self) -> None:
        code, out = self.run_with(FakeRun(), which=None)
        self.assertEqual(1, code)
        self.assertIn("error: .: claude CLI not on PATH", out)
        self.assertNotIn("validated with", out)

    def test_main_fails_when_validate_fails(self) -> None:
        fake = FakeRun()
        fake.answer("plugin", "validate", "plugins/beta", output="✘ broken\n")
        code, out = self.run_with(fake)
        self.assertEqual(1, code)
        self.assertIn("error: plugins/beta: claude plugin validate plugins/beta", out)
        self.assertIn("ok: alpha 0.1.0 (main: 0.1.0)", out)
        self.assertNotIn("ok: beta", out)

    def test_main_validates_only_the_selected_mods(self) -> None:
        fake = FakeRun()
        self.run_with(fake, "--mod", "beta")
        self.assertEqual(
            [
                ["--version"],
                ["plugin", "validate", "."],
                ["plugin", "validate", "plugins/beta"],
            ],
            fake.commands(),
        )


if __name__ == "__main__":
    unittest.main()
