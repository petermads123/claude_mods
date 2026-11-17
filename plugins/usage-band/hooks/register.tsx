import { atom, read, update } from 'claude-code'
import type { Hook, Register } from 'claude-code'

import type { Fable, Limit } from '../types'

const limits = atom({ plugin: 'usage-band', key: 'limits' } as const, [])
const fable = atom({ plugin: 'usage-band', key: 'fable' } as const, null)

const LABELS: Record<string, string> = { five_hour: '5h', seven_day: 'week', fable: 'fable' }

// Where /usage gets its figures. Not a published API: a reply this code cannot read hides the bar
const USAGE_URL = 'https://api.anthropic.com/api/oauth/usage'
const REFRESH_MS = 30 * 60 * 1000

// Pale green → pale yellow → pale red along the bar
const STOPS: [number, number, number][] = [
  [156, 207, 156],
  [232, 214, 140],
  [232, 152, 144],
]
const TRACK = '#4A4A47'
const MIN_TILES = 5
const MAX_TILES = 40
// Cells per tile with its gap; above 2 because ▰ draws about 3.1 cells wide with its gap on the
// desktop font, which a full-width row of tiles shows up
const TILE_CELLS = 3.2
// label, percent, reset time and their gaps
const FIXED = 20
const GAP = 4

const hex = (n: number): string => Math.round(n).toString(16).padStart(2, '0')

// The colour at position t (0 to 1) along the bar
const tone = (t: number): string => {
  const x = Math.max(0, Math.min(1, t)) * (STOPS.length - 1)
  const k = Math.min(STOPS.length - 2, Math.floor(x))
  const f = x - k
  const [a, b] = [STOPS[k]!, STOPS[k + 1]!]
  return `#${a.map((v, j) => hex(v + (b[j]! - v) * f)).join('')}`
}

const short = (iso: string | undefined, now: number): string => {
  if (!iso) return ''
  const mins = Math.max(0, Math.round((Date.parse(iso) - now) / 60000))
  if (mins >= 1440) return `${Math.floor(mins / 1440)}d ${Math.floor((mins % 1440) / 60)}h`
  if (mins >= 60) return `${Math.floor(mins / 60)}h ${String(mins % 60).padStart(2, '0')}m`
  return `${mins}m`
}

// The weekly Fable window in the usage service's reply, or null when it holds none
const fableIn = (text: string, now: number): Fable | null => {
  let body: unknown

  try {
    body = JSON.parse(text)
  } catch {
    return null
  }

  const list = (body as { limits?: unknown } | null)?.limits
  if (!Array.isArray(list)) return null

  for (const l of list) {
    const name = l?.scope?.model?.display_name
    if (l?.group !== 'weekly' || typeof name !== 'string' || !/fable/i.test(name)) continue
    if (typeof l.percent !== 'number') continue

    return {
      percentUsed: l.percent,
      resetsAt: typeof l.resets_at === 'string' ? l.resets_at : undefined,
      fetchedAt: now,
    }
  }

  return null
}

// A stored figure in the shape this version writes, or null
const kept = (v: unknown): Fable | null => {
  const f = v as Fable | null
  return f && typeof f.percentUsed === 'number' && typeof f.fetchedAt === 'number' ? f : null
}

// Asks the usage service at most once per REFRESH_MS, across sessions
const refresh = async ($: Parameters<Hook<'session.start'>>[0]): Promise<void> => {
  try {
    const now = await $.clock.now()
    const last = kept(await $.store.get('fable'))

    if (last && now - last.fetchedAt < REFRESH_MS) {
      await update($, fable, () => last)
      return
    }

    const auth = await $.session.authorize()
    if (!auth || auth.kind !== 'bearer') return

    const res = await $.http.fetch(USAGE_URL, {
      auth: auth.handle,
      headers: { 'anthropic-beta': 'oauth-2025-04-20' },
    })
    if (!res.ok) return

    // A reply without the window still counts as asked, so it is not asked again at once
    const got = fableIn(res.text, now) ?? { percentUsed: 0, fetchedAt: now }

    await update($, fable, () => got)
    await $.store.set('fable', got)
  } catch {
    // The bar stays as it was
  }
}

export const register: Register = on => {
  // Remembered across sessions, so a new session shows the last figures before its first reply
  on('session.start', async ($, e, next) => {
    const usage = await $.session.usage()
    let start: Limit[] = usage.rateLimits

    if (start.length === 0) {
      const now = await $.clock.now()
      const saved = ((await $.store.get('limits')) ?? []) as Limit[]
      start = saved.filter(l => !l.resetsAt || Date.parse(l.resetsAt) > now)
    }

    await update($, limits, () => start)
    await refresh($)

    return next(e)
  })

  on('session.measure', async ($, e, next) => {
    if (e.changed.includes('rateLimits') && e.rateLimits.length > 0) {
      await update($, limits, () => e.rateLimits)
      await $.store.set('limits', e.rateLimits)
      await refresh($)
    }

    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const all: Limit[] = await read($, limits)
    const shown = all.filter(l => l.kind in LABELS)

    if (e.props.hasSurvey || shown.length === 0) {
      return next(e)
    }

    const now = await $.clock.now()
    const f: Fable | null = await read($, fable)

    // Only once Fable has been used in a week that is still running
    if (f && f.percentUsed > 0 && (!f.resetsAt || Date.parse(f.resetsAt) > now)) {
      shown.push({ kind: 'fable', percentUsed: f.percentUsed, resetsAt: f.resetsAt })
    }

    const { Box, Text } = $.ui.resolve(e)
    const cols = e.viewport?.columns ?? 100
    // Three bars: the 5-hour window on a row of its own, the two weekly windows beneath
    const rows = shown.length > 2 ? [shown.slice(0, 1), shown.slice(1)] : [shown]

    // Other mods' bands draw above these bars, whichever order the engine runs the hooks in
    const above = await next(e)

    return (
      <Box flexDirection="column">
        {above}
        {rows.map((row, r) => {
          const each = (cols - GAP * (row.length - 1)) / row.length
          const tiles = Math.max(MIN_TILES, Math.min(MAX_TILES, Math.floor((each - FIXED) / TILE_CELLS)))

          return (
            <Box key={String(r)} gap={GAP}>
              {row.map(l => {
                const pct = Math.max(0, Math.min(100, l.percentUsed))
                const filled = Math.round((pct / 100) * tiles)

                return (
                  <Box key={l.kind} flexGrow={1} width={`${100 / row.length}%`} gap={1}>
                    <Text dimColor>{LABELS[l.kind]}</Text>
                    <Box gap={1}>
                      {Array.from({ length: tiles }, (_, i) => (
                        <Text key={String(i)} color={i < filled ? tone(i / Math.max(1, tiles - 1)) : TRACK}>
                          ▰
                        </Text>
                      ))}
                    </Box>
                    <Text color={tone(pct / 100)}>{Math.round(pct)}%</Text>
                    <Text dimColor>{short(l.resetsAt, now)}</Text>
                  </Box>
                )
              })}
            </Box>
          )
        })}
      </Box>
    )
  })
}
