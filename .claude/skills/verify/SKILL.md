---
name: verify
description: Check the marketplace and its mods — marketplace.json and every plugin.json agree, each changed mod's version is raised above main's with a changelog section, the README lists every mod, generated types are not tracked, and claude plugin validate and the mods' tests pass. Use before every commit and pull request, after changing anything under plugins/, or when the user asks whether the mods are in order.
argument-hint: [mod]
---

# /verify — check the mods

No `model` or `effort` here on purpose: `/mod` and `/release` run this skill mid-turn, and a pin
would switch the rest of their turn to it.

1. Run `python scripts/check_mods.py`, adding `--mod <mod>` when one was named. It checks the
   marketplace file, each mod's files, version and changelog against `main`, the README and the
   generated types, then runs `claude plugin validate` on the marketplace and each mod and
   `claude plugin test` on each mod that has tests.
2. Fix every `error:` line at its cause, then run the script again:

   | The error says | Fix |
   |---|---|
   | changed since main but version is still X | raise the version once, by the table in `.claude/rules/mods.md`, and add its changelog section — once the user has agreed the bump (`/mod` step 1) |
   | version is not X.Y.Z | three whole numbers without leading zeros, no `v`, no suffix |
   | no CHANGELOG.md section for X | add `## X — YYYY-MM-DD` at the top of the mod's `CHANGELOG.md` |
   | a folder under plugins/ that no entry lists | add `{ "name": "<mod>", "source": "./plugins/<mod>" }` to `.claude-plugin/marketplace.json` |
   | version lives in plugin.json only | delete `version` from the marketplace entry |
   | README does not mention `<mod>@claude-mods` | add the mod's row and install line to `README.md` |
   | generated types are tracked | `git rm --cached -r plugins/<mod>/.claude-plugin/types` |
   | author missing | `"author": { "name": "Peter" }` in the mod's `plugin.json` |
   | description missing | in the file the error names: the mod's own `plugin.json`, or the top of `marketplace.json`. `claude plugin validate --strict` fails without either |
   | modules must name exactly one hooks module | one entry in `hooks/hooks.json`, such as `["./register.tsx"]` |
   | types must start with "./" | `"types": "./types/index.d.ts"`, forward slashes |
   | a name Claude Code reserves | rename the mod before it ships; see Names in `.claude/rules/mods.md` |
   | git ... failed, or shares no history with HEAD | fix git first, as the error says (a shallow clone: `git fetch --unshallow`); never read it as "nothing changed" |
   | claude plugin validate / test failed | read the output it quotes; the plugin-authoring skill's reference explains the engine's refusals |

   An error that comes back a second time for the same reason means the fix was a guess: stop
   and tell the user what fails and what you tried. So does a fix that needs the user's decision,
   such as a bump on an edit nobody agreed.
3. Report warnings too. Fix the ones that are plainly slips, such as a version that jumps past
   the next one or a changelog section out of order; leave the rest to the user.
4. Report one line per mod — `<mod>: main <old> → <new>, validated, <n> test files` or `no
   tests` — then the script's last lines, including the Claude Code version that validated.
   If the script said the `claude` CLI was missing, say plainly that nothing was validated.

The report is this run's output. Never report the mods as checked from memory or from an earlier
run.
