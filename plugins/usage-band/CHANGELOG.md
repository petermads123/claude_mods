# usage-band

## 0.2.0 — 2026-11-17

- Shows the weekly Fable window as a third bar, labelled `fable`, once Fable usage is above 0%.
  The mod asks Anthropic's usage service for the figure through the session's own login, at
  session start and then at most once every 30 minutes, when usage has moved. If the service
  cannot be reached the bar is left out.
- With three bars, the 5-hour bar takes a full-width row of its own and the two weekly bars sit
  side by side beneath it.
- The bars are sized to the band's own width, start at the same column and end at the same
  column on both rows, and the reset time no longer wraps onto a second line.
- Remembers the last Fable figure across sessions under a new stored value; the stored 5-hour and
  weekly figures are kept as they were.

## 0.1.1 — 2026-11-04

- Leaves room for other mods' bands: anything another mod draws above the prompt now shows
  above the usage bars instead of possibly being hidden by them.

## 0.1.0 — 2026-11-02

- Shows the 5-hour and weekly usage windows above the prompt as tiled bars, shaded from green to
  red as each window fills, with the time left until it resets.
- Remembers the last figures across sessions, so a new session shows them before its first reply.
