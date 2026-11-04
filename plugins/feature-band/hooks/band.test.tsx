import type { On } from 'claude-code'
import { expect, test } from 'claude-code/testing'

const BAND = {
  plugin: 'feature-band',
  component: 'AbovePrompt',
  props: {
    hasSurvey: false,
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

const seed = (progress: unknown) => ({ value: { value: progress, version: 1 } })

// Stands for the engine's own drawing beneath every plugin.
const engine = (on: On) =>
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)

    return <Text>engine</Text>
  })

test('round and step are shown while a feature is in flight', async ($, on) => {
  engine(on)
  on('state.get', () =>
    seed({
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
    }),
  )

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
  on('state.get', () =>
    seed({
      title: 'CSV export',
      rounds: [{ name: 'Writer core', status: 'running' }],
      current: 1,
      step: 1,
      steps: STEPS,
      status: 'blocked',
      note: 'asked about delimiters',
      final: false,
    }),
  )

  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect((await ui.find({ type: 'Text', text: /waiting/ }))?.text).toBe('waiting on a question')
  await ui.unmount()
})

test('a fix being diagnosed shows its own bar instead of the round bar', async ($, on) => {
  engine(on)
  on('state.get', () =>
    seed({
      title: 'CSV quoting',
      rounds: [],
      current: 0,
      step: 2,
      steps: ['Reproduce', 'Hunt', 'Root cause', 'Critique'],
      status: 'running',
      note: 'three hunters out',
      final: false,
      diagnosis: true,
    }),
  )

  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect((await ui.find({ type: 'Text', text: 'diagnose' }))?.text).toBe('diagnose')
  expect((await ui.find({ type: 'Text', text: '2/4' }))?.text).toBe('2/4')
  expect((await ui.find({ type: 'Text', text: 'Hunt' }))?.text).toBe('Hunt')
  expect(await ui.find({ type: 'Text', text: 'round' })).toBeUndefined()
  expect((await ui.find({ type: 'Text', text: 'engine' }))?.text).toBe('engine')
  await ui.unmount()
})

test('nothing in flight leaves the band to the others', async ($, on) => {
  on('state.get', () => seed(null))
  engine(on)

  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect((await ui.find({ type: 'Text', text: 'engine' }))?.text).toBe('engine')
  expect(await ui.find({ type: 'Text', text: /round/ })).toBeUndefined()
  await ui.unmount()
})
