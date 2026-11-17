import { expect, test } from 'claude-code/testing'

const NOW = Date.parse('2026-11-17T10:00:00Z')
const BAND = {
  plugin: 'usage-band',
  component: 'AbovePrompt',
  props: {
    hasSurvey: false,
    isCollapsed: false,
    isWorking: false,
    maxRows: 10,
    bodyColumns: 120,
    scroll: { offset: 0, bodyRows: 10 },
    view: {},
  },
  viewport: { columns: 120, rows: 40 },
  surface: 'terminal',
} as const

const LIMITS = [
  { kind: 'five_hour', percentUsed: 41, resetsAt: '2026-11-17T13:00:00Z' },
  { kind: 'seven_day', percentUsed: 59, resetsAt: '2026-11-20T05:00:00Z' },
]

const reply = (percent: number): string =>
  JSON.stringify({
    limits: [
      { kind: 'session', group: 'session', percent: 41, scope: null },
      { kind: 'weekly_all', group: 'weekly', percent: 59, scope: null },
      {
        kind: 'weekly_scoped',
        group: 'weekly',
        percent,
        resets_at: '2026-11-20T05:00:00Z',
        scope: { model: { id: null, display_name: 'Fable' }, surface: null },
      },
    ],
  })

// Stands for the engine: the session's windows, the clock, the store and the usage service
const engine = (on: any, opts: { stored?: unknown; text?: string; ok?: boolean }) => {
  const calls = { fetches: [] as { url: string; init: any }[], stored: undefined as unknown }

  on('session.start', (_: unknown, e: any) => ({ cwd: e.cwd }))
  on('session.usage', () => ({ value: { rateLimits: LIMITS, context: {} } }))
  on('clock.now', () => ({ value: NOW }))
  on('store.get', (_: unknown, e: any) => ({ value: e.key === 'fable' ? opts.stored : undefined }))
  on('store.set', (_: unknown, e: any) => {
    if (e.key === 'fable') calls.stored = e.value
    return { value: undefined }
  })
  on('session.authorize', () => ({ value: { handle: 'h', kind: 'bearer' } }))
  on('http.fetch', (_: unknown, e: any) => {
    calls.fetches.push({ url: e.url, init: e.init })
    return { value: { status: opts.ok === false ? 500 : 200, ok: opts.ok !== false, headers: {}, text: opts.text ?? '' } }
  })
  on('ui.render', ($: any, e: any) => {
    const { Box } = $.ui.resolve(e)

    return <Box />
  })

  return calls
}

const labels = async (ui: any): Promise<string[]> => {
  const found: string[] = []
  for (const text of ['5h', 'week', 'fable']) {
    if (await ui.find({ type: 'Text', text: new RegExp(`^${text}$`) })) found.push(text)
  }
  return found
}

test('Fable above 0% draws a third bar, fetched with the session login', async ($, on) => {
  const calls = engine(on, { text: reply(12) })

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount(BAND)

  expect(await labels(ui)).toEqual(['5h', 'week', 'fable'])
  expect((await ui.find({ type: 'Text', text: /12%$/ }))?.text.trim()).toBe('12%')
  expect(calls.fetches.length).toBe(1)
  expect(calls.fetches[0]?.init?.auth).toBe('h')
  expect((calls.stored as any)?.percentUsed).toBe(12)
  await ui.unmount()
})

test('Fable at 0% draws no bar', async ($, on) => {
  engine(on, { text: reply(0) })

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount(BAND)

  expect(await labels(ui)).toEqual(['5h', 'week'])
  await ui.unmount()
})

test('a failed fetch leaves the two bars', async ($, on) => {
  engine(on, { ok: false })

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount(BAND)

  expect(await labels(ui)).toEqual(['5h', 'week'])
  await ui.unmount()
})

test('a figure under 30 minutes old is shown without asking again', async ($, on) => {
  const calls = engine(on, {
    stored: { percentUsed: 30, resetsAt: '2026-11-20T05:00:00Z', fetchedAt: NOW - 29 * 60 * 1000 },
    text: reply(99),
  })

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount(BAND)

  expect(calls.fetches.length).toBe(0)
  expect((await ui.find({ type: 'Text', text: /30%$/ }))?.text.trim()).toBe('30%')
  await ui.unmount()
})

test('a figure 30 minutes old is asked for again', async ($, on) => {
  const calls = engine(on, {
    stored: { percentUsed: 30, fetchedAt: NOW - 30 * 60 * 1000 },
    text: reply(31),
  })

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

  expect(calls.fetches.length).toBe(1)
})
