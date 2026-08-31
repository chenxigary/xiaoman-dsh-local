/**
 * Security gate for Codex turn execution.
 *
 * The bridge uses two pinned App Server processes: a managed auth-only
 * process and a turn process with an isolated, credential-free HOME. Access
 * tokens are delivered only over private stdio and are verified not to be
 * persisted before a turn can start. The turn sandbox remains read-only.
 */

export type CodexTurnCapability = 'read-only' | 'unavailable'

export function codexTurnCapability(): CodexTurnCapability {
  return 'read-only'
}

export function assertCodexTurnAvailable(capability: CodexTurnCapability): void {
  if (capability === 'read-only') return
  throw Object.assign(new Error('Codex turn execution is unavailable'), {
    code: 'security_isolation_unavailable',
  })
}

export async function guardedCodexStart<T>(
  capability: CodexTurnCapability,
  start: () => Promise<T>,
): Promise<T> {
  // Keep the gate and coordinator invocation in one seam so a future caller
  // cannot accidentally append/claim before the security decision.
  assertCodexTurnAvailable(capability)
  return await start()
}
