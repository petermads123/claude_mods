# Working in this repo

This repo is **claude-mods**, a Claude Code plugin marketplace holding Peter's mods: plugins whose
hooks module changes Claude Code's own interface or behaviour. Machines install the mods from it
(`claude plugin install <mod>@claude-mods`), and every change to what a mod ships raises its
version so those machines pick the change up. The repository is public on GitHub as
`petermads123/claude_mods`.

## Map

```
.claude-plugin/marketplace.json   the marketplace "claude-mods": one entry per mod, name and source only
plugins/<mod>/                    one folder per mod; layout in .claude/rules/mods.md
scripts/check_mods.py             the checker: marketplace, mods, versions, README, validate, tests
tests/                            unittest suites for the checker and the hooks
.claude/rules/mods.md             mod conventions, loaded whenever a file under plugins/ is read
.claude/skills/                   /mod, /verify, /release, /sync-mods
.claude/hooks/                    guard_git.py (PreToolUse), mod_gate.py (Stop)
.github/workflows/check.yml       CI: the checker without the CLI, and the tests
README.md                         the mod list and the install lines
```

The mods themselves are listed in `.claude-plugin/marketplace.json`, the one source of truth;
`README.md` repeats them for people, and the checker holds the two together.

## The pipeline

| Stage | Skill, step | Waits for |
|---|---|---|
| Agree the change: the mod, what it does, the version bump | `/mod` 1 | the user |
| Branch, bump, build, verify, commit | `/mod` 2–5 | — |
| Try it live | `/mod` 6 | the user |
| Push and open a pull request | `/release` 1–3 | the user's yes, before publishing |
| Merge, tag the release, delete the branch | `/release` 4–5 | the user's instruction to merge |

`/verify` runs the checks on their own. `/sync-mods` installs or updates the mods on a machine.

The waits are the user's. Never take the confirmation at `/mod` 1, the "it works" at `/mod` 6,
the yes at `/release` 2 or the instruction to merge for them: not because the answer looks
obvious, not to save time.

## Routing

The user never has to type a slash command. Classify what they ask before touching anything, say
the route in one line, then take it:

| Situation | Use |
|---|---|
| A new mod, a change to a mod, a bug in a mod, bringing in a mod from `~/.claude/dev-mods` or `~/.claude/mods` | `/mod` |
| Whether the mods are in order; before any commit | `/verify` |
| Publishing finished work, opening the pull request, closing out a merged one | `/release` |
| Installing or updating the mods on this machine, setting up another machine | `/sync-mods` |
| This repo's own tooling or docs — the checker, the hooks, the skills, CI, the README | a `chore/` or `docs/` branch off an up-to-date `main`, the change, `python -m unittest discover -s tests` and `/verify`, a commit, then `/release` |
| A question | an answer, not a pipeline |

An explicit slash command wins over the routing.

## Versions — the important part

- A mod's version lives in its `plugin.json` and nowhere else; never in `marketplace.json`.
- A branch that changes anything a mod ships raises that mod's version above `main`'s, once,
  with a `CHANGELOG.md` section for the new version. What ships and the bump table are in
  `.claude/rules/mods.md`. Mods are versioned one by one, never together.
- Machines that installed from GitHub only take a change that comes with a new version, so an
  unbumped change never reaches them. Never lower a version: Claude Code applies any different
  version as an update, a lower one included.
- After each merge, `claude plugin tag` marks every released version on `main` as
  `<mod>--v<version>`.
- The checker, the Stop hook and CI all refuse a changed mod whose version has not moved.

## Branches

Never commit to `main`: `.claude/hooks/guard_git.py` refuses the command. Branch names are
`type/kebab-case`, a mod's branch carries the mod's name, and the commit type matches the prefix:

| Prefix | Use for |
|---|---|
| `feat/` | a new mod (`feat/<mod>`); new or changed behaviour or looks (`feat/<mod>-<slug>`) |
| `fix/` | a defect (`fix/<mod>-<slug>`) |
| `refactor/` | a mod's code restructured, behaviour unchanged (`refactor/<mod>-<slug>`) |
| `chore/` | the checker, hooks, skills, CI, settings |
| `docs/` | prose only |

The prefix says what kind of change it is; the bump table says how far the version moves. A
colour tweak is a `feat/` branch and a patch bump.

## Review and merge

The approver is `petermads123`. **Claude never merges on its own judgment, and never on an
approval alone**: a pull request reaches `main` because the approver pressed the button, or told
Claude in so many words to merge it. Once told, the instruction waives nothing: nothing pushed
since they looked, `check` green, no conflict. Claude merges with a merge commit. After a merge,
Claude deletes the branch once `git merge-base --is-ancestor` confirms its head is on `main`; if
the button squashed or rebased, the head is not on `main` and Claude asks first. A branch closed
without merging is left alone. `/release` holds the steps.

## Commands

From the repo root:

```
python scripts/check_mods.py                 # everything, including claude plugin validate and tests
python scripts/check_mods.py --mod <mod>     # one mod, plus the marketplace-wide checks
python scripts/check_mods.py --no-cli        # the quick half, as CI and the Stop hook run it
python -m unittest discover -s tests -v      # the tooling's own tests (several minutes on Windows)
claude plugin validate --strict plugins/<mod>
claude plugin test plugins/<mod>             # only for a mod with *.test.ts(x); it fails on one without
```

## Automation in place

- **Before every `Bash` or `PowerShell` call**, `guard_git.py` refuses a commit or push that would
  land on `main`. `settings.json` pre-approves the git commands the pipeline uses, the checker,
  the tests and the read-only `claude plugin` and `gh` commands; the guard is what makes that safe.
  Tagging, opening and merging pull requests and changing plugin installs still ask, on purpose:
  each follows a yes from the user.
- **Before a turn ends**, `mod_gate.py` runs `check_mods.py --changed --no-cli` on the checkout
  Claude is working in, a worktree included. On a work branch it blocks the turn once while the
  checker reports an error, and at the next stop only shows what still fails. On `main` or a
  detached HEAD it only shows the errors, since nothing there can be committed. When a fix needs
  the user — a bump they have not agreed, a check that failed twice — tell them what it reported
  rather than fixing past their decision. Create `.claude/.skip-gate` to bypass it deliberately.
- **On every pull request and push to `main`**, CI runs `check_mods.py --no-cli` and the tests.

Claude Code watches its settings files, so edits to hooks and permissions apply to the running
session, and the hook scripts run fresh on every call. Skills and rules reload without a restart
too.

The tooling is Python, standard library only, for Python 3.10 and later. `guard_git.py` is
vendored from the template repo, whose suite covers it; a fix to it goes there first.
