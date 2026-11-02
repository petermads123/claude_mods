# claude_mods

My Claude Code mods — plugins that change Claude Code's own interface and behaviour — kept as a
plugin marketplace, so any machine can install them and update them to each new version.

## The mods

| Mod | What it does | Install |
|---|---|---|
| `usage-band` | Shows the 5-hour and weekly usage windows above the prompt | `claude plugin install usage-band@claude-mods` |

They are checked with the Claude Code CLI 2.1.287 and run in the desktop app on 2.1.309; an older
Claude Code may not load them.

## Install

Once per machine, add the marketplace:

```bash
claude plugin marketplace add petermads123/claude_mods
```

Then install each mod you want:

```bash
claude plugin install usage-band@claude-mods
```

At the prompt of a terminal session the same two steps are `/plugin marketplace add
petermads123/claude_mods` and `/plugin install usage-band@claude-mods`. In the desktop app, where
`/plugin` does not run, click **+** next to the prompt box, then **Plugins** (in some versions
**Add plugins**), add the marketplace `petermads123/claude_mods`, then add the mod. From Claude
Code 2.1.292 a shell does both steps in one: `claude plugin install usage-band --marketplace
petermads123/claude_mods`.

A session that is already open loads a mod installed from a shell once you run `/reload-plugins`
in it, or in the next session.

## Update

```bash
claude plugin marketplace update claude-mods
claude plugin update <mod>@claude-mods
```

The first command fetches the repo; the second, run for each mod you installed, replaces the
installed copy when the mod's version has changed, which every change to a mod's code makes it do.
Then restart Claude Code, or run `/reload-plugins` in an open session.

**Auto-update** is off for this marketplace until you turn it on: in a terminal session, `/plugin`
→ **Marketplaces** → `claude-mods` → **Enable auto-update**, or add `"autoUpdate": true` beside
`"source"` in the `claude-mods` entry of `extraKnownMarketplaces` in `~/.claude/settings.json`.
Interactive terminal sessions then update in the background and ask you to run `/reload-plugins`.
Sessions the desktop app starts appear to run with Claude Code's auto-updater disabled
(`DISABLE_AUTOUPDATER`); Claude Code documents `FORCE_AUTOUPDATE_PLUGINS=1` as the setting that
keeps plugin updates on regardless, untried here. Without it, run the two commands above.

## Develop

On the machine you work on the mods from, register your clone instead of GitHub:

```bash
claude plugin marketplace add "<path to your clone>"
```

Claude Code then runs the mods straight from the clone: edit a mod, run `/reload-plugins`, and the
change is live with no reinstall. That includes whatever branch the clone has checked out. Open
Claude Code in the clone and describe what you want; the `/mod` workflow in
[CLAUDE.md](CLAUDE.md) takes it from there, and `/sync-mods` sets a machine up either way.
