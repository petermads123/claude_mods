import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Progress, RoundView } from '../types'

const progress = atom({ plugin: 'feature-band', key: 'progress' } as const, null)

// The template's pipeline state: development/<branch>/brief.json and r<n>-<slug>/round.json.
// The schemas are documented in the template's development/README.md.
const ROOT = 'development'
const LIVE = ['concept', 'rounds', 'final']
const MAX_DEPTH = 4
const POLL_MS = 4000

const TRACK = '#4A4A47'
const DONE = '#9CCF9C'
const ACTIVE = '#8FB8E8'
const WAITING = '#E8D68C'
const FAILED = '#E89890'
const MIN_TILES = 5
const MAX_TILES = 40
const TILE_CELLS = 2.5
// label, count and gaps before the name
const FIXED = 14

type Json = Record<string, unknown>

const str = (v: unknown): string => (typeof v === 'string' ? v : '')
const num = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? v : 0)
const obj = (v: unknown): Json | null =>
  v !== null && typeof v === 'object' && !Array.isArray(v) ? (v as Json) : null

/** Every brief.json under development/, newest first, found a few folders deep. */
const findBriefs = async ($: EngineInterface): Promise<{ path: string; mtimeMs: number }[]> => {
  const found: { path: string; mtimeMs: number }[] = []
  const walk = async (dir: string, depth: number): Promise<void> => {
    const entries = await $.fs.list(dir).catch(() => [])
    for (const entry of entries) {
      const path = `${dir}/${entry.name}`
      if (entry.kind === 'file' && entry.name === 'brief.json') {
        found.push({ path, mtimeMs: entry.mtimeMs })
      } else if (entry.kind === 'dir' && depth < MAX_DEPTH) {
        await walk(path, depth + 1)
      }
    }
  }
  await walk(ROOT, 1)

  return found.sort((a, b) => b.mtimeMs - a.mtimeMs)
}

const readJson = async ($: EngineInterface, path: string): Promise<Json | null> => {
  try {
    return obj(JSON.parse(await $.fs.read(path)))
  } catch {
    return null
  }
}

/** The feature in flight in this project, or null. */
const scan = async ($: EngineInterface): Promise<Progress | null> => {
  if (!(await $.fs.exists(ROOT).catch(() => false))) return null

  for (const { path } of await findBriefs($)) {
    const brief = await readJson($, path)
    if (brief === null || !LIVE.includes(str(brief.status))) continue

    const rawRounds = Array.isArray(brief.rounds) ? brief.rounds.map(obj) : []
    const entries = rawRounds.filter((r): r is Json => r !== null && num(r.n) > 0)
    const rounds: RoundView[] = entries.map(r => ({ name: str(r.name), status: str(r.status) }))
    const current = num(brief.current_round)
    const entry = entries.find(r => num(r.n) === current)
    const folder = path.slice(0, path.lastIndexOf('/'))
    const state = entry ? await readJson($, `${folder}/r${num(entry.n)}-${str(entry.slug)}/round.json`) : null
    const steps = Array.isArray(state?.steps) ? state.steps.filter((s): s is string => typeof s === 'string') : []

    return {
      title: str(brief.title) || str(brief.feature),
      rounds,
      current,
      step: num(state?.step),
      steps,
      status: str(state?.status) || str(brief.status),
      note: str(state?.note),
      final: str(brief.status) === 'final',
    }
  }

  return null
}

const refresh = async ($: EngineInterface): Promise<void> => {
  const next = await scan($).catch(() => null)
  const now = await read($, progress)
  if (JSON.stringify(next) !== JSON.stringify(now)) {
    await update($, progress, () => next)
  }
}

const stateColor = (status: string): string =>
  status === 'blocked' ? WAITING : status === 'failed' ? FAILED : ACTIVE

export const register: Register = on => {
  let timer: { cancel: () => void } | undefined

  on('session.start', async ($, e, next) => {
    timer?.cancel()
    await refresh($)
    timer = $.clock.every(POLL_MS, () => {
      void refresh($)
    })

    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    await refresh($)

    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const p: Progress | null = await read($, progress)

    if (e.props.hasSurvey || p === null || p.rounds.length === 0) {
      return next(e)
    }

    const below = await next(e)
    const { Box, Text } = $.ui.resolve(e)
    const cols = e.props.bodyColumns ?? e.viewport?.columns ?? 100
    const tiles = Math.max(MIN_TILES, Math.min(MAX_TILES, Math.floor((cols / 2 - FIXED) / TILE_CELLS)))

    const merged = p.rounds.filter(r => r.status === 'merged').length
    const total = p.rounds.length
    const stepCount = p.steps.length
    const stepsDone = p.status === 'done' ? stepCount : Math.max(0, p.step - 1)
    const roundDone = p.final ? total : merged + (stepCount ? stepsDone / stepCount : 0)
    const color = stateColor(p.status)
    const name = p.final ? 'final check' : (p.rounds[p.current - 1]?.name ?? '')

    const bar = (fraction: number, fill: string, key: string) => {
      const filled = Math.round(Math.max(0, Math.min(1, fraction)) * tiles)
      return (
        <Box key={key} gap={1}>
          {Array.from({ length: tiles }, (_, i) => (
            <Text key={String(i)} color={i < filled ? fill : TRACK}>
              ▰
            </Text>
          ))}
        </Box>
      )
    }

    const waiting = p.status === 'blocked' ? 'waiting on a question' : p.status === 'failed' ? 'failed' : ''

    return (
      <Box flexDirection="column">
        <Box gap={1}>
          <Text dimColor>round</Text>
          {bar(roundDone / total, p.final ? DONE : ACTIVE, 'rounds')}
          <Text color={p.final ? DONE : ACTIVE}>
            {Math.min(p.current, total)}/{total}
          </Text>
          <Text bold>{name}</Text>
          <Text dimColor wrap="truncate-end">
            {p.title}
          </Text>
        </Box>
        {!p.final && stepCount > 0 && p.step > 0 ? (
          <Box gap={1}>
            <Text dimColor>step </Text>
            {bar(stepsDone / stepCount, color, 'steps')}
            <Text color={color}>
              {Math.min(p.step, stepCount)}/{stepCount}
            </Text>
            <Text bold>{p.steps[p.step - 1] ?? ''}</Text>
            <Text color={waiting ? color : undefined} dimColor={!waiting} wrap="truncate-end">
              {waiting || p.note}
            </Text>
          </Box>
        ) : null}
        {below}
      </Box>
    )
  })
}
