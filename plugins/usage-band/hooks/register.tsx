import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { Limit } from '../types'

const limits = atom({ plugin: 'usage-band', key: 'limits' } as const, [])

const LABELS: Record<string, string> = { five_hour: '5h', seven_day: 'week' }

// Pale green → pale yellow → pale red along the bar
const STOPS: [number, number, number][] = [
  [156, 207, 156],
  [232, 214, 140],
  [232, 152, 144],
]
const TRACK = '#4A4A47'
const MIN_TILES = 5
const MAX_TILES = 40
// Cells per tile with its gap; above 2 because ▰ draws wider than a cell on the desktop font
const TILE_CELLS = 2.5
// label, percent, reset time and their gaps
const FIXED = 22

const hex = (n: number): string => Math.round(n).toString(16).padStart(2, '0')

// The colour at position t (0 to 1) along the bar
const tone = (t: number): string => {
  const x = Math.max(0, Math.min(1, t)) * (STOPS.length - 1)
  const k = Math.min(STOPS.length - 2, Math.floor(x))
  const f = x - k
  const [a, b] = [STOPS[k], STOPS[k + 1]]
  return `#${a.map((v, j) => hex(v + (b[j] - v) * f)).join('')}`
}

const short = (iso: string | undefined, now: number): string => {
  if (!iso) return ''
  const mins = Math.max(0, Math.round((Date.parse(iso) - now) / 60000))
  if (mins >= 1440) return `${Math.floor(mins / 1440)}d ${Math.floor((mins % 1440) / 60)}h`
  if (mins >= 60) return `${Math.floor(mins / 60)}h ${String(mins % 60).padStart(2, '0')}m`
  return `${mins}m`
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

    return next(e)
  })

  on('session.measure', async ($, e, next) => {
    if (e.changed.includes('rateLimits') && e.rateLimits.length > 0) {
      await update($, limits, () => e.rateLimits)
      await $.store.set('limits', e.rateLimits)
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
    const { Box, Text } = $.ui.resolve(e)
    const cols = e.viewport?.columns ?? 100
    const half = (cols - 4 * (shown.length - 1)) / shown.length
    const tiles = Math.max(MIN_TILES, Math.min(MAX_TILES, Math.floor((half - FIXED) / TILE_CELLS)))

    return (
      <Box gap={4}>
        {shown.map(l => {
          const pct = Math.max(0, Math.min(100, l.percentUsed))
          const color = tone(pct / 100)
          const filled = Math.round((pct / 100) * tiles)

          return (
            <Box key={l.kind} flexGrow={1} width="50%" gap={1}>
              <Text dimColor>{LABELS[l.kind]}</Text>
              <Box gap={1}>
                {Array.from({ length: tiles }, (_, i) => (
                  <Text key={String(i)} color={i < filled ? tone(i / Math.max(1, tiles - 1)) : TRACK}>
                    ▰
                  </Text>
                ))}
              </Box>
              <Text color={color}>{Math.round(pct)}%</Text>
              <Text dimColor>{short(l.resetsAt, now)}</Text>
            </Box>
          )
        })}
      </Box>
    )
  })
}
