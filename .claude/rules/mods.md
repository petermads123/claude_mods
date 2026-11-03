---
paths:
  - "plugins/**"
---

# Mod conventions

Loaded whenever a file under `plugins/` is read or edited. `/mod` is the procedure for a change;
these are the rules every change to a mod follows, however it started.

## Before writing mod code

Load the `plugin-authoring` skill first. It carries this build's API (a types file of tens of
thousands of lines: grep it for the event or noun at hand), worked examples, the event list and
the long-form reference. Its "where to write it" names `~/.claude/dev-mods/`; that does not apply
here. In this repo a mod is written in `plugins/<mod>/`, the folder the marketplace serves it from.

## A mod's folder

| Path | Holds |
|---|---|
| `.claude-plugin/plugin.json` | `name` (the folder's name), `version`, `description`, `author` (`{ "name": "Peter" }`), and `types` when the mod keeps `$.state` |
| `hooks/hooks.json` | `{ "modules": ["./register.tsx"] }`: one path, relative to `hooks/` |
| `hooks/register.tsx` | `export const register: Register = (on, options) => { ... }` |
| `types/index.d.ts` | the `$.state` contract: each value declared under the mod's name in `interface PluginState`. It exports types only; even a bare `export {}` fails validation |
| `CHANGELOG.md` | one `## X.Y.Z — YYYY-MM-DD` section per version, newest first |
| `hooks/*.test.ts(x)` | optional tests, run by `claude plugin test plugins/<mod>` |

Keep `"types"` in `plugin.json` for a mod with state: Claude Code before 2.1.299 does not find
the contract without it. Paths in `plugin.json` and `hooks.json` start with `./` and use forward
slashes; Claude Code refuses a mod whose `types` path does not.

`.claude-plugin/types/` inside a mod is written by Claude Code when it loads the mod with
`--plugin-dir` in an interactive session, or when Claude writes the mod; it may also write a
`tsconfig.json` at the mod's top level. Never edit or commit `.claude-plugin/types/`
(`.gitignore` keeps it out). Commit the `tsconfig.json` or delete it: `claude plugin tag` refuses
to tag while it sits untracked.

## Names

A mod's name is kebab-case and is its install id everywhere, `<mod>@claude-mods`. Avoid renaming
one: it changes that id on every machine, and what the mod kept in `$.store` stays under the old
id. If it must happen, add `"renames": { "<old>": "<new>" }` at the top of `marketplace.json` and
never remove the line; Claude Code then moves each machine's enabled setting to the new name.
Check on one machine whether `claude plugin install <new>@claude-mods` is still needed.

Claude Code reserves some plugin names: never `claude`, `anthropic`, `anthropics`, `claude-code`
or `claude-mods`, nothing starting `claude-`, `anthropic-`, `anthropics-` or `cc-plugin-`, and
nothing pairing `official` with `claude` or `anthropic`.

## Versions

`version` in the mod's `plugin.json` is the only version there is. Never put one in the mod's
`marketplace.json` entry.

- A branch that changes anything a mod ships raises that mod's version above `main`'s, once per
  branch, at the start of the work. Everything in `plugins/<mod>/` ships except `CHANGELOG.md`,
  `README.md`, `tsconfig.json`, `*.test.ts(x)` and the generated `.claude-plugin/types/`.
- Versions are `X.Y.Z` exactly: three whole numbers without leading zeros (`0.1.10`, never
  `0.1.01`), no `v`, no suffix. A new mod starts at `0.1.0`.
- Each mod has its own version. Never bump a mod the branch does not change.
- Every bump adds a `CHANGELOG.md` section for the new version, saying in the user's terms what
  changed.
- Never lower a version. Claude Code treats any version that differs from the installed one as
  an update, so a lower number would roll every machine back to it.

| Bump | Moves | For |
|---|---|---|
| patch | x.y.**Z** | a fix; a tweak to how it looks or reads; a refactor that changes no behaviour |
| minor | x.**Y**.0 | new or changed behaviour; a new option, hook, command or element |
| major | **X**.0.0 | a rewrite, or a large new capability such as a pane or a tool for the model |

Why it matters: a machine that installed the marketplace from GitHub runs a copy cached under
the version, and `claude plugin update` replaces that copy only when `plugin.json` names a
different version. A change shipped without a bump never reaches the machines that already have
the mod, while a machine that installs fresh gets the new code under the old number, so two
machines run different code that claims to be the same version.

## Stored values outlive versions

`$.store` keeps values across sessions, and `$.state` keeps them across reloads within a session,
so new code meets what old code stored. When a change alters the shape of a stored value:

- for a `$.state` value, declare it `Shaped<T>` in the contract and give its `atom` a new `shape`
  tag: a value stored in the old shape is then read as the atom's initial value;
- for a `$.store` key, check what `$.store.get` returns before using it, or move to a new key.

Either way, the changelog section says what earlier values are no longer kept.

## Public repository

Anyone can read every file in this repo. A mod holds no keys, tokens, internal hostnames or URLs,
and no one else's data. A new or imported mod is scanned with the plugin-authoring skill's
`scan_secrets` tool before it is committed.

## Marketplace entry and README

A mod's entry in `.claude-plugin/marketplace.json` is `{ "name": "<mod>", "source":
"./plugins/<mod>" }`. If it carries a `description`, it is the same text as `plugin.json`'s.
`README.md` lists every mod with its install line, `claude plugin install <mod>@claude-mods`.

## Checks

`python scripts/check_mods.py` (or `/verify`) checks everything above that a script can check,
then runs `claude plugin validate --strict` and the mods' tests. The Stop hook runs the quick
half — no CLI — whenever a turn ends: on a work branch it blocks the turn once while an error
stands, and on `main` it only shows the errors.
