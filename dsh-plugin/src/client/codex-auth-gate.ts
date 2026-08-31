import type { CodexAuthStatus } from './codex-remote-client.ts'

export type CodexAuthGateStatus = Pick<CodexAuthStatus, 'state'> & Partial<Pick<CodexAuthStatus, 'capability'>>

/** Credential isolation failures must fail closed at the mode selector. */
export function canSelectCodex(state: CodexAuthGateStatus | null): boolean {
  return state !== null && state.state !== 'unavailable' && state.capability !== 'unavailable'
}

/** Login is meaningful only when the Host reports an ordinary signed-out user. */
export function canShowCodexLogin(state: CodexAuthGateStatus | null): boolean {
  return state?.state === 'signed_out'
}
