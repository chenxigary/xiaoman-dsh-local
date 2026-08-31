/** Synchronous ownership fence for an async Codex start RPC. */
export interface CodexStartGate {
  readonly claimed: boolean
  tryClaim(): boolean
  release(): void
}

export function createCodexStartGate(): CodexStartGate {
  let claimed = false
  return {
    get claimed() { return claimed },
    tryClaim() {
      if (claimed) return false
      claimed = true
      return true
    },
    release() { claimed = false },
  }
}
