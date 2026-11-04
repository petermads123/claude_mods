# feature-band

## 0.1.0 — 2026-11-04

- Shows two bars above the prompt while a feature from the template's pipeline is being built:
  the round (how many rounds are merged, the current one's name) and the step within it (its
  name and one-line note), amber while the round waits on a question and red when it failed.
- Reads `development/**/brief.json` and the current round's `round.json` every few seconds
  and after each turn; shows nothing when no feature is in flight, so on `main` or once the
  pull request is open it stays out of the way.
- Sits above the usage bars when `usage-band` is installed too.
