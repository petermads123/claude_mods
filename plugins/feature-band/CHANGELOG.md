# feature-band

## 0.3.0 — 2026-11-20

- Shows a third bar, `tests`, on top of the round and step bars while pytest runs: how many
  tests are through out of those collected, green tiles for the ones through and red for the
  failed, the passed (✓), failed (✗) and skipped (○) counts, and the test running now. It
  updates every second, stays for a few seconds after the run with the result, then goes.
- Needs a repo on template 0.2.4 or later, whose pytest runs write
  `.pytest_cache/progress.json`. It shows for any pytest run there, also with no feature in
  flight; without that file nothing changes.
- Nothing stored by earlier versions is affected: the test run is a new value.

## 0.2.0 — 2026-11-04

- Shows a fix's diagnosis as its own bar (`diagnose`, with the step: Reproduce, Hunt, Root
  cause, Critique) while the template's `/fix` is diagnosing, before any round exists; amber
  while it waits on a question.

## 0.1.0 — 2026-11-04

- Shows two bars above the prompt while a feature from the template's pipeline is being built:
  the round (how many rounds are merged, the current one's name) and the step within it (its
  name and one-line note), amber while the round waits on a question and red when it failed.
- Reads `development/**/brief.json` and the current round's `round.json` every few seconds
  and after each turn; shows nothing when no feature is in flight, so on `main` or once the
  pull request is open it stays out of the way.
- Sits above the usage bars when `usage-band` is installed too.
