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

declare module 'claude-code' {
  interface PluginState {
    'feature-band': { progress: Progress | null }
  }
}
