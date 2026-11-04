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
}

declare module 'claude-code' {
  interface PluginState {
    'feature-band': { progress: Progress | null }
  }
}
