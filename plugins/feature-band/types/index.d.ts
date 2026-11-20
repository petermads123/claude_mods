export type RoundView = { name: string; status: string }

export type Progress = {
  title: string
  rounds: RoundView[]
  current: number
  step: number
  steps: string[]
  status: string
  note: string
  final: boolean
  // A fix being diagnosed: step and steps are the diagnosis's, rounds is empty.
  // Absent in values stored by 0.1.0, which read as not diagnosing.
  diagnosis?: boolean
}

// A pytest run in flight, or one that ended a moment ago, read from the template's
// .pytest_cache/progress.json. New in 0.3.0.
export type TestRun = {
  total: number
  done: number
  passed: number
  failed: number
  skipped: number
  // The id of the test running now.
  current: string
  finished: boolean
}

declare module 'claude-code' {
  interface PluginState {
    'feature-band': { progress: Progress | null; tests: TestRun | null }
  }
}
