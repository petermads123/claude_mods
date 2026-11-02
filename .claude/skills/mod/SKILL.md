---
name: mod
description: Start or continue work on a mod in this marketplace — a new mod, a change or fix to one that exists, or bringing in a mod written elsewhere (~/.claude/dev-mods, ~/.claude/mods). Agrees the change and its version bump with the user, branches, writes the mod with the plugin-authoring skill, verifies, commits, has the user try it live, then hands to /release. Use for any change under plugins/, whether the user names the skill or just describes the work in prose.
argument-hint: <what to build or change>
model: opus
effort: high
---

# /mod — the pipeline for a mod

Every change to a mod goes through these steps. Two wait for the user: agreeing the change
(step 1) and seeing it work (step 6). `/release` adds a third, a yes before anything is published.
Everything between is yours, without asking.

| # | Step | Waits for |
|---|---|---|
| 0 | Orient | — |
| 1 | Agree the change and the bump | the user |
| 2 | Branch and bump | — |
| 3 | Build | — |
| 4 | Verify | — |
| 5 | Commit | — |
| 6 | Try it live | the user |
| 7 | Release: `/release` | the user, before publishing |

`.claude/rules/mods.md` loads as soon as a file under `plugins/` is read; it holds the rules these
steps rely on: the folder layout, the bump table, stored values, the public-repo rule.

## 0. Orient

- `git fetch origin`, `git status --short --branch`, and
  `git rev-list --left-right --count origin/main...HEAD` for how far the branch is behind and
  ahead of `main` (`git status` compares with the branch's own upstream instead).
- **Work in flight** is a branch other than `main` with commits ahead of `origin/main`, or
  uncommitted changes under `plugins/`. If it is this mod's work, resume where its state says:
  - version not yet raised → step 1, unless the user agreed this change and its bump earlier in
    this conversation. An edit nobody agreed, such as the user's own change in the clone, gets
    its bump from the user like any other. Then raise the version on the branch that holds the
    work; on `main`, step 2 carries the uncommitted work onto the new branch.
  - raised but not built, or checks failing → step 3 or 4;
  - committed → step 6, or `/release` if a pull request is already open for the branch
    (`gh pr list --head <branch>`).

  If it is another mod's, ask whether to finish that first or set it aside (commit it on its
  branch) and start this one from `main`.
- Read `.claude-plugin/marketplace.json`; for an existing mod, its `plugin.json`, `CHANGELOG.md`
  and code.

## 1. Agree the change (waits for the user)

Settle these in conversation, then put them to the user as one short summary to confirm:

- **Mod** — an existing one, or a new name: kebab-case, not already in the marketplace, not one
  Claude Code reserves (see Names in the rule file: nothing starting `claude-`), saying what the
  mod is. It becomes `<mod>@claude-mods` on every machine, so pick a name to keep: renaming
  later changes the install id everywhere.
- **What changes**, as what the user will see or what will happen, not in code terms. For a new
  mod: where it shows (a band above the prompt, a pane, the status line, a toast, a slash command,
  a hook on tool calls) and what it shows or does.
- **The bump** — patch, minor or major by the table in `.claude/rules/mods.md`, and the version
  that makes: `main`'s version raised once. A new mod starts at `0.1.0`.
- **Stored values** — whether the change alters anything the mod keeps in `$.state` or
  `$.store`, and if so what happens to what earlier versions stored.
- **Branch** — `feat/<mod>` for a new mod, `feat/<mod>-<slug>` for new or changed behaviour or
  looks, `fix/<mod>-<slug>` for a defect, `refactor/<mod>-<slug>` for the same behaviour in new
  code. The prefix names the kind of change, the bump its size: a colour tweak is `feat/` and a
  patch.

A defect is reproduced and its cause found before anything is agreed: read the code, ask what the
user saw, and look in the debug log (`claude --debug` writes a line for every hook that failed or
result the engine refused). The summary then says what is wrong and why, and the fix is agreed on
the cause rather than on the symptom.

End the turn on the question. Do not start step 2 until the user confirms; "just do it" for a
small change is a confirmation.

## 2. Branch and bump

- `git switch main`, `git pull --ff-only`, then `git switch -c <branch>`. Uncommitted work on
  `main` comes along onto the new branch.
- Set `version` in `plugins/<mod>/.claude-plugin/plugin.json`.
- Add the new section at the top of `plugins/<mod>/CHANGELOG.md`: `## <version> — <today as
  YYYY-MM-DD>`, then a bullet per change in the words agreed at step 1.

For an existing mod the bump comes first, so the tree is consistent from the start: on a work
branch the Stop hook blocks a turn that ends with a changed mod whose version has not moved. For a
new mod this step is only the branch; step 3 writes its `plugin.json` at `0.1.0` and its first
changelog section.

## 3. Build

- Load the `plugin-authoring` skill before writing any mod code. Use its API reference, examples
  and types, and write in `plugins/<mod>/`, not in the `dev-mods` folder it names.
- **New mod:** write `.claude-plugin/plugin.json` (`name`, `version` `0.1.0`, `description`,
  `author` `{ "name": "Peter" }`), `hooks/hooks.json`, `hooks/register.tsx`, `types/index.d.ts`
  if the mod keeps state (with `"types": "./types/index.d.ts"` in `plugin.json`) and
  `CHANGELOG.md`. Add `{ "name": "<mod>", "source": "./plugins/<mod>" }` to
  `.claude-plugin/marketplace.json`, and the mod's row and install line to `README.md`.
- **Bringing in a mod written elsewhere** (a session's `~/.claude/dev-mods/<session>/<mod>/`, or
  `~/.claude/mods/<mod>/`): copy its folder to `plugins/<mod>/`, leaving out any `.git` and
  `.claude-plugin/types/`. Then treat it as a new mod: keep its version if it has a valid one,
  else `0.1.0`; register it; changelog section "Brought into the marketplace".
- **New or brought-in code is public.** Run the plugin-authoring skill's `scan_secrets` tool on
  the repo folder before reading copied files, leave out anything it flags until the user decides,
  then read every file for keys, internal URLs or anyone else's data.
- Logic worth pinning down — a calculation, a state transition, a guard's verdict — gets a
  `hooks/<name>.test.ts(x)` in the style of the plugin-authoring example `pane.test.tsx`.

## 4. Verify

Run `python scripts/check_mods.py --mod <mod>`. Fix what it reports and run it again. If a check
fails a second time for the same reason, the fix was a guess: stop, and tell the user what fails
and what you tried.

## 5. Commit

Run `/verify` on the whole tree, then commit the mod together with the marketplace file and the
README when they changed. Message: `<type>(<mod>): <what changed> (v<version>)`, for example
`feat(usage-band): show the Opus window (v0.2.0)`.

## 6. Try it live (waits for the user)

The checks prove the mod loads and keeps its contract. Only the user can say it does what was
agreed.

- `claude plugin marketplace list --json`: `claude-mods` with `"source": "directory"` and a
  `path` that is this clone means this machine runs the mods straight from this folder. Not
  registered, from GitHub, or from another folder: offer `/sync-mods` first.
- `claude plugin list --json`: is `<mod>@claude-mods` installed and enabled? Its `version` is the
  one recorded at install, not necessarily the folder's; newer CLIs also print `readFromFolder`.
- Installed: ask the user to run `/reload-plugins` (or open a new session) and look.
- A new mod: with the user's yes, since it changes their Claude Code setup,
  `claude plugin install <mod>@claude-mods`. Then ask the user to run `/reload-plugins` (or open
  a new session) and look: a mod installed from a shell does not load in a session that is
  already open until then.
- The user says what they see. If it is not what was agreed, go back to step 3 on the same branch
  and keep the version: it has already moved once on this branch.

## 7. Release

Hand to `/release`.
