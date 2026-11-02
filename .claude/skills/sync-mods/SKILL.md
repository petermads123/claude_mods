---
name: sync-mods
description: Install or update this repo's mods on the current machine — registers the claude-mods marketplace (this clone as a folder marketplace where mods are developed, or the GitHub repo elsewhere), installs every mod it lists, moves mods off an older marketplace such as local-mods, and updates what is installed. Use when the user sets up a machine, asks to install or update their mods, or wants the mods here to match the repo.
model: sonnet
effort: medium
---

# /sync-mods — the mods on this machine

Everything here changes the user's own Claude Code setup (their user settings and plugin cache),
so nothing is added, installed, uninstalled or removed before the user says yes to the plan.

## 1. Read what is there

- `claude plugin marketplace list --json` and `claude plugin list --json`.
- `.claude-plugin/marketplace.json` in this clone: the mods there are.
- Work out: is a marketplace named `claude-mods` registered, and from where? The marketplace list
  says: `"source": "directory"` with a `path` (this folder or another), or GitHub. `claude plugin
  list` shows only a cache path even for a folder marketplace, so do not judge the source by it.
  Which `<mod>@claude-mods` are installed and enabled, at which recorded version? Is any mod of the
  same name installed from another marketplace — for example `usage-band@local-mods`, the folder
  `~/.claude/mods` this repo replaced?

## 2. Propose (waits for the user)

Two ways to have the marketplace, and a machine has one of them:

| | Folder (this clone) | GitHub |
|---|---|---|
| Add with | `claude plugin marketplace add "<absolute path of this clone>"` | `claude plugin marketplace add petermads123/claude_mods` |
| A change arrives | when the files change: `/reload-plugins` re-reads the folder | only through a new version: `claude plugin update` |
| Runs | whatever is checked out here, including a work branch | the version installed |
| For | the machine the mods are developed on | every other machine |

Recommend the folder on a machine that has this clone and is used to work on mods, GitHub
anywhere else. Say what the folder costs: switching this clone to a branch with a broken mod
breaks the mod for every session that reloads.

Say too what moving a mod between marketplaces costs. A mod's `$.store` data is kept per
install id, so `<mod>@claude-mods` starts with nothing the old `<mod>@<other>` stored (for
`usage-band`, only the last usage figures, which come back with the next reply). Retiring an old
marketplace such as `local-mods` with `claude plugin marketplace remove` uninstalls everything
installed from it and deletes those plugins' saved options and data. `claude-mods` itself is
never removed, only re-pointed (step 3).

On a GitHub machine, offer auto-update as well: `"autoUpdate": true` beside `"source"` in the
`claude-mods` entry of `extraKnownMarketplaces` in `~/.claude/settings.json`. It is off by
default for this marketplace, and sessions the desktop app starts appear to run with the
auto-updater disabled, so on a machine used through the app the update in step 4 is still the way.

Lay out the exact steps — each command below that applies, in order — and wait for the yes.

## 3. Apply

- Register the marketplace with the chosen source's `claude plugin marketplace add`, if it is not
  registered from that source already.
- If `claude-mods` is registered from the other source, re-point it; never remove it, which would
  uninstall every mod from it and delete their saved options and data. First set `source` in the
  `claude-mods` entry of `extraKnownMarketplaces` in `~/.claude/settings.json` to the chosen one —
  `{ "source": "directory", "path": "<absolute path of this clone>" }` or
  `{ "source": "github", "repo": "petermads123/claude_mods" }` — and change nothing else there
  (newer Claude Code refuses an add that contradicts that entry). Then run the matching
  `claude plugin marketplace add`. It answers that the marketplace now points at the new source,
  and the installed mods stay installed.
- For each mod in `marketplace.json` that is not installed:
  `claude plugin install <mod>@claude-mods`.
- For each mod installed from another marketplace under the same name:
  `claude plugin uninstall <mod>@<that marketplace>`. Then, if that marketplace has nothing else
  installed, `claude plugin marketplace remove <that marketplace>`. Its folder on disk is left
  where it is; say so, and leave deleting it to the user.
- Always name the marketplace (`<mod>@claude-mods`). While a mod of the same name is installed
  from two marketplaces, a bare `<mod>` is resolved inconsistently: `install` picks one,
  `disable` the other, and `update` refuses.
- Turn on auto-update if the user said yes to it: edit `~/.claude/settings.json` by hand, adding
  `"autoUpdate": true` to the `claude-mods` entry and nothing else.

## 4. Update

When everything is registered already, bring it up to date:

- `claude plugin marketplace update claude-mods` first, so every mod is compared with the
  repository as it is now (`claude plugin update` refreshes it too, but skips the refresh within
  about 30 seconds of the last one);
- for each installed mod, `claude plugin update <mod>@claude-mods`;
- report each mod's version before and after.

On a folder marketplace the mods run straight from the folder, and `/reload-plugins` picks up
file changes. After a mod's version has moved, still run `claude plugin update <mod>@claude-mods`
there: it changes nothing that runs, but re-records the version that `claude plugin list --json`
reports (newer CLIs show the folder's own as `folderVersion`).

## 5. Finish

Tell the user to restart Claude Code, or run `/reload-plugins` in an open session, so the changes
take effect, then check with `claude plugin list --json` that each mod is installed, enabled and
at the version expected.
