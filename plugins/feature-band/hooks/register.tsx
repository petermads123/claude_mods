import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Progress, RoundView, TestRun } from '../types'

const progress = atom({ plugin: 'feature-band', key: 'progress' } as const, null)
const tests = atom({ plugin: 'feature-band', key: 'tests' } as const, null)

// The template's pipeline state: development/<branch>/brief.json and r<n>-<slug>/round.json.
// The schemas are documented in the template's development/README.md.
const ROOT = 'development'
const LIVE = ['diagnosis', 'concept', 'rounds', 'final']
const MAX_DEPTH = 4
const POLL_MS = 4000

// A pytest run's live progress, written by the template's .claude/hooks/pytest_progress.py.
const TESTS_FILE = '.pytest_cache/progress.json'
const TESTS_POLL_MS = 1000
// A finished run stays on show this long.
const LINGER_MS = 15000
// A run that has written nothing for this long was killed, not slow.
const STALE_MS = 120000

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
    const diagnosis = str(brief.status) === 'diagnosis'
    const state = diagnosis
      ? await readJson($, `${folder}/diagnosis/state.json`)
      : entry
        ? await readJson($, `${folder}/r${num(entry.n)}-${str(entry.slug)}/round.json`)
        : null
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
      diagnosis,
    }
  }

  return null
}

/** The pytest run to show at `nowMs`: one in flight, or one that ended a moment ago. */
export const liveRun = (raw: Json | null, nowMs: number): TestRun | null => {
  if (raw === null || num(raw.updated) <= 0) return null

  const finished = raw.finished === true
  const age = nowMs - num(raw.updated) * 1000
  if (age > (finished ? LINGER_MS : STALE_MS)) return null

  return {
    total: num(raw.total),
    done: num(raw.done),
    passed: num(raw.passed),
    failed: num(raw.failed),
    skipped: num(raw.skipped),
    current: str(raw.current),
    finished,
  }
}

const refreshTests = async ($: EngineInterface): Promise<void> => {
  const raw = (await $.fs.exists(TESTS_FILE).catch(() => false)) ? await readJson($, TESTS_FILE) : null
  const next = liveRun(raw, await $.clock.now())
  const now = await read($, tests)
  if (JSON.stringify(next) !== JSON.stringify(now)) {
    await update($, tests, () => next)
  }
}

/** `tests/test_csv.py::test_quotes[a]` as `test_csv.py::test_quotes[a]`. */
const shortId = (nodeid: string): string => {
  const [file = '', ...rest] = nodeid.split('::')
  return [file.slice(file.lastIndexOf('/') + 1), ...rest].join('::')
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
  let testsTimer: { cancel: () => void } | undefined

  on('session.start', async ($, e, next) => {
    timer?.cancel()
    testsTimer?.cancel()
    await refresh($)
    await refreshTests($).catch(() => undefined)
    timer = $.clock.every(POLL_MS, () => {
      void refresh($)
    })
    testsTimer = $.clock.every(TESTS_POLL_MS, () => {
      void refreshTests($).catch(() => undefined)
    })

    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    await refresh($)

    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const stored: Progress | null = await read($, progress)
    const t: TestRun | null = await read($, tests)
    const p = stored !== null && (stored.rounds.length > 0 || stored.diagnosis) ? stored : null

    if (e.props.hasSurvey || (p === null && t === null)) {
      return next(e)
    }

    const below = await next(e)
    const { Box, Text } = $.ui.resolve(e)
    const cols = e.props.bodyColumns ?? e.viewport?.columns ?? 100
    const tiles = Math.max(MIN_TILES, Math.min(MAX_TILES, Math.floor((cols / 2 - FIXED) / TILE_CELLS)))

    // The test run sits on top: green tiles for tests that are through, red for the failed.
    const testRow = () => {
      if (t === null) return null

      const share = (n: number) => (t.total > 0 ? Math.round((Math.min(n, t.total) / t.total) * tiles) : 0)
      const red = t.failed > 0 ? Math.max(1, share(t.failed)) : 0
      const filled = Math.max(red, share(t.done))
      const color = t.failed > 0 ? FAILED : t.finished ? DONE : ACTIVE
      const tail = !t.finished
        ? shortId(t.current)
        : t.failed > 0
          ? 'finished with failures'
          : t.total > 0
            ? 'all passed'
            : 'no tests ran'

      return (
        <Box gap={1}>
          <Text dimColor>tests</Text>
          <Box gap={1}>
            {Array.from({ length: tiles }, (_, i) => (
              <Text key={String(i)} color={i < filled - red ? DONE : i < filled ? FAILED : TRACK}>
                ▰
              </Text>
            ))}
          </Box>
          <Text color={color}>
            {t.done}/{t.total}
          </Text>
          <Text color={DONE}>✓ {t.passed}</Text>
          <Text color={t.failed > 0 ? FAILED : undefined} dimColor={t.failed === 0} bold={t.failed > 0}>
            ✗ {t.failed}
          </Text>
          {t.skipped > 0 ? <Text dimColor>○ {t.skipped}</Text> : null}
          <Text dimColor wrap="truncate-end">
            {tail}
          </Text>
        </Box>
      )
    }

    if (p === null) {
      return (
        <Box flexDirection="column">
          {testRow()}
          {below}
        </Box>
      )
    }

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

    // A fix being diagnosed has no rounds yet: one bar for the diagnosis's own steps.
    if (p.diagnosis) {
      return (
        <Box flexDirection="column">
          {testRow()}
          <Box gap={1}>
            <Text dimColor>diagnose</Text>
            {bar(stepCount ? stepsDone / stepCount : 0, color, 'diagnosis')}
            <Text color={color}>
              {Math.min(Math.max(p.step, 1), stepCount || 1)}/{stepCount || '?'}
            </Text>
            <Text bold>{p.steps[p.step - 1] ?? ''}</Text>
            <Text color={waiting ? color : undefined} dimColor={!waiting} wrap="truncate-end">
              {waiting || p.note || p.title}
            </Text>
          </Box>
          {below}
        </Box>
      )
    }

    return (
      <Box flexDirection="column">
        {testRow()}
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
