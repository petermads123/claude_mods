export type Limit = { kind: string; percentUsed: number; resetsAt?: string }

// The weekly Fable window as the usage service last gave it, and when it was asked
export type Fable = { percentUsed: number; resetsAt?: string; fetchedAt: number }

declare module 'claude-code' {
  interface PluginState {
    'usage-band': { limits: Limit[]; fable: Fable | null }
  }
}
