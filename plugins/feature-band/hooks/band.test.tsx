import type { On } from 'claude-code'
import { expect, test } from 'claude-code/testing'

import { liveRun } from './register'

const BAND = {
  plugin: 'feature-band',
  component: 'AbovePrompt',
  props: {
    hasSurvey: false,
    isCollapsed: false,
    isWorking: false,
    maxRows: 10,
    bodyColumns: 100,
    scroll: { offset: 0, bodyRows: 10 },
    view: {},
  },
} as const

const STEPS = [
  'Conceptualize', 'Plan', 'Implement', 'Verify', 'Test design',
  'Write tests', 'Run tests', 'Concept check', 'Close',
]

// What the band's two values hold: the pipeline's progress and the pytest run.
const state = (on: On, progress: unknown, tests: unknown = null) =>
  on('state.get', (_, e) => ({ value: { value: e.key === 'tests' ? tests : progress, version: 1 } }))

// Stands for the engine's own drawing beneath every plugin.
const engine = (on: On) =>
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)

    return <Text>engine</Text>
  })

test('round and step are shown while a feature is in flight', async ($, on) => {
  engine(on)
  state(on, {
      title: 'CSV export',
      rounds: [
        { name: 'Writer core', status: 'merged' },
        { name: 'CLI', status: 'running' },
        { name: 'Docs', status: 'planned' },
      ],
      current: 2,
      step: 4,
      steps: STEPS,
      status: 'running',
      note: 'ruff and mypy clean',
      final: false,
  })

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...BAND, surface })
    expect((await ui.find({ type: 'Text', text: '2/3' }))?.text).toBe('2/3')
    expect((await ui.find({ type: 'Text', text: 'CLI' }))?.text).toBe('CLI')
    expect((await ui.find({ type: 'Text', text: '4/9' }))?.text).toBe('4/9')
    expect((await ui.find({ type: 'Text', text: 'Verify' }))?.text).toBe('Verify')
    expect((await ui.find({ type: 'Text', text: /ruff and mypy/ }))?.text).toBe('ruff and mypy clean')
    // whatever draws beneath (the usage bars, the engine) still shows, below the band
    expect((await ui.find({ type: 'Text', text: 'engine' }))?.text).toBe('engine')
    await ui.unmount()
  }
})

test('a round waiting on a question says so', async ($, on) => {
  engine(on)
  state(on, {
      title: 'CSV export',
      rounds: [{ name: 'Writer core', status: 'running' }],
      current: 1,
      step: 1,
      steps: STEPS,
      status: 'blocked',
      note: 'asked about delimiters',
      final: false,
  })

  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect((await ui.find({ type: 'Text', text: /waiting/ }))?.text).toBe('waiting on a question')
  await ui.unmount()
})

test('a fix being diagnosed shows its own bar instead of the round bar', async ($, on) => {
  engine(on)
  state(on, {
      title: 'CSV quoting',
      rounds: [],
      current: 0,
      step: 2,
      steps: ['Reproduce', 'Hunt', 'Root cause', 'Critique'],
      status: 'running',
      note: 'three hunters out',
      final: false,
      diagnosis: true,
  })

  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect((await ui.find({ type: 'Text', text: 'diagnose' }))?.text).toBe('diagnose')
  expect((await ui.find({ type: 'Text', text: '2/4' }))?.text).toBe('2/4')
  expect((await ui.find({ type: 'Text', text: 'Hunt' }))?.text).toBe('Hunt')
  expect(await ui.find({ type: 'Text', text: 'round' })).toBeUndefined()
  expect((await ui.find({ type: 'Text', text: 'engine' }))?.text).toBe('engine')
  await ui.unmount()
})

test('nothing in flight leaves the band to the others', async ($, on) => {
  state(on, null)
  engine(on)

  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect((await ui.find({ type: 'Text', text: 'engine' }))?.text).toBe('engine')
  expect(await ui.find({ type: 'Text', text: /round/ })).toBeUndefined()
  await ui.unmount()
})

const FEATURE = {
  title: 'CSV export',
  rounds: [{ name: 'Writer core', status: 'running' }],
  current: 1,
  step: 7,
  steps: STEPS,
  status: 'running',
  note: '',
  final: false,
}

const RUN = {
  total: 61,
  done: 37,
  passed: 35,
  failed: 2,
  skipped: 0,
  current: 'tests/test_csv.py::test_quotes',
  finished: false,
}

test('a test run shows on top of the round and step, with its counts', async ($, on) => {
  engine(on)
  state(on, FEATURE, RUN)

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...BAND, surface })
    expect((await ui.find({ type: 'Text', text: 'tests' }))?.text).toBe('tests')
    expect((await ui.find({ type: 'Text', text: '37/61' }))?.text).toBe('37/61')
    expect((await ui.find({ type: 'Text', text: /✓/ }))?.text).toBe('✓ 35')
    expect((await ui.find({ type: 'Text', text: /✗/ }))?.text).toBe('✗ 2')
    expect((await ui.find({ type: 'Text', text: /test_quotes/ }))?.text).toBe('test_csv.py::test_quotes')
    expect(await ui.find({ type: 'Text', text: /○/ })).toBeUndefined()
    // the round and step bars are still there, and so is what draws beneath
    expect((await ui.find({ type: 'Text', text: '7/9' }))?.text).toBe('7/9')
    expect((await ui.find({ type: 'Text', text: 'Run tests' }))?.text).toBe('Run tests')
    expect((await ui.find({ type: 'Text', text: 'engine' }))?.text).toBe('engine')
    await ui.unmount()
  }
})

test('a test run shows with no feature in flight, and says how it ended', async ($, on) => {
  engine(on)
  state(on, null, { ...RUN, done: 61, passed: 58, failed: 0, skipped: 3, finished: true })

  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect((await ui.find({ type: 'Text', text: '61/61' }))?.text).toBe('61/61')
  expect((await ui.find({ type: 'Text', text: /○/ }))?.text).toBe('○ 3')
  expect((await ui.find({ type: 'Text', text: 'all passed' }))?.text).toBe('all passed')
  expect(await ui.find({ type: 'Text', text: 'round' })).toBeUndefined()
  expect((await ui.find({ type: 'Text', text: 'engine' }))?.text).toBe('engine')
  await ui.unmount()
})

test('a run is live while it writes, lingers briefly once finished, then goes', () => {
  const raw = { ...RUN, updated: 1000 }
  const at = (seconds: number) => (1000 + seconds) * 1000

  expect(liveRun(raw, at(1))?.done).toBe(37)
  expect(liveRun(raw, at(119))?.done).toBe(37)
  // nothing written for two minutes: the run was killed
  expect(liveRun(raw, at(121))).toBeNull()

  const ended = { ...raw, finished: true }
  expect(liveRun(ended, at(14))?.finished).toBe(true)
  expect(liveRun(ended, at(16))).toBeNull()

  expect(liveRun(null, at(0))).toBeNull()
  // a file that is not a progress record
  expect(liveRun({ hello: 'world' }, at(0))).toBeNull()
  // missing counts read as zero rather than failing the band
  expect(liveRun({ updated: 1000 }, at(1))).toEqual({
    total: 0, done: 0, passed: 0, failed: 0, skipped: 0, current: '', finished: false,
  })
})
