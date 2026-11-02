---
name: release
description: Publish finished mod work — verify the branch, then, once the user says yes, push it and open a pull request to main; merge only on the user's explicit instruction; after the merge, tag each released mod version, delete the branch and say how each machine picks the update up. Use when /mod's work is committed and the user has seen it working, or to close out a pull request that has merged.
argument-hint: [PR number, when closing out]
model: sonnet
effort: medium
---

# /release — from a branch to every machine

## 1. Ready

- On a work branch, never `main`.
- `git fetch origin`, then `git rev-list --left-right --count origin/main...HEAD`: the first
  number is how far the branch is behind `main`. If it is behind, `git merge origin/main` and
  resolve.
- If `main` has meanwhile raised the version of a mod this branch also raises, the changelog
  conflicts: keep `main`'s sections exactly as they are, and put this branch's section above
  them as `## <the next version above main's> — <today>`. Set that version in the mod's
  `plugin.json`, then commit the merge and the re-bump together:
  `<type>(<mod>): raise to v<new> above main's v<old>`.
- `/verify` passes.
- Last, `git status --porcelain` is empty: commit what is left, or ask the user about anything
  that is not this work.

## 2. Ask (waits for the user)

Show the branch, each changed mod as `<mod>: <main's version> → <new version>` with its changelog
section, and the commits (`git log --oneline origin/main..HEAD`). Say that the repository is
public, so the code will be readable by anyone. Then ask once: push, open the pull request, and
tag the release when it merges. Nothing leaves this machine before the yes.

## 3. Push and open the pull request

- `git push -u origin <branch>`.
- `gh pr create --base main --head <branch>`, titled like the commit for a single change —
  `<type>(<mod>): <what changed> (v<version>)`, with the version `plugin.json` holds now, which a
  re-bump may have moved past the first commit's — or naming every mod for several. The body has a
  section per mod (its version change and changelog lines), the `/verify` summary, and ends with
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- `gh pr edit <n> --add-assignee @me`. GitHub refuses a review request from the pull request's
  own author, so in this solo repo the assignment is what puts it in the approver's queue.
- Give the user the link. CI runs `check` on the pull request; read it with `gh pr checks <n>`.

## 4. Merge — only on the user's explicit instruction

Never merge on your own judgment, on an approval alone, or because everything is green. The
approver is `petermads123`; a merge happens because they pressed the button, or told you in so
many words ("merge it") here or in a pull request comment. "Looks good" is feedback, not an
instruction.

Once told, check before merging, and if one fails say which and wait:

- nothing was pushed after they looked;
- `gh pr view <n> --json mergeable,mergeStateStatus,statusCheckRollup` shows no conflict and
  `check` passed.

Then `gh pr merge <n> --merge`.

## 5. After the merge — whoever pressed the button

- `gh pr view <n> --json state,mergedAt` says `MERGED`. A pull request closed without merging is
  left alone, branch and all.
- `git switch main`, `git pull --ff-only`.
- For each mod the pull request added or whose version it raised:
  `claude plugin tag plugins/<mod> --push`. It creates and pushes the tag `<mod>--v<version>`,
  after checking that `plugin.json` and the marketplace entry agree; it refuses while the tree
  holds uncommitted or untracked changes to the mod.
- Delete the branch: `git fetch origin --prune`; if `origin/<branch>` still exists,
  `git merge-base --is-ancestor origin/<branch> origin/main` must succeed before
  `git push origin --delete <branch>`, then `git branch -d <branch>`. If it does not succeed, the
  button squashed or rebased the pull request: ask the user before deleting either branch.
- Tell the user how the update arrives:
  - on a machine where the marketplace is this clone (a folder marketplace), it is live now:
    run `/reload-plugins` or start a new session. `claude plugin update <mod>@claude-mods` there
    changes nothing that runs; it only re-records the version `claude plugin list` reports;
  - on a machine that installed from GitHub: `claude plugin marketplace update claude-mods`,
    then `claude plugin update <mod>@claude-mods`, then restart Claude Code or run
    `/reload-plugins`. Where auto-update is on, an interactive terminal session does this in the
    background and asks for `/reload-plugins`; sessions the desktop app starts do not;
  - `/sync-mods` does either, from a clone of this repo.
