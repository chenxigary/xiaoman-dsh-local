import type { AgentMode } from '../agent-mode.ts'

/** QQ has no authenticated session identity, so Codex mode is deny-by-default. */
export function acceptsQqInbound(mode: AgentMode, sessionId: string | undefined): boolean {
  return mode === 'dsh' && sessionId !== undefined && sessionId.trim() !== ''
}

/** Outbound history is also owner-scoped and native DSH-only. */
export function acceptsQqOutbound(mode: AgentMode, sessionId: string | undefined): boolean {
  return acceptsQqInbound(mode, sessionId)
}
