/** Package-owned durable Codex lifecycle invariant. */

import type { Context } from '@deepseek-ai/cordis'
import type { InvariantFailure, InvariantInstaller } from '@deepseek-ai/dsh-invariants'
import type { Session, SessionEvent } from '@deepseek-ai/dsh-session'

const PACKAGE_NAME = '@deepseek-ai/dsh-host-codex'

export const name = 'host-codex-invariant'
export const inject = ['invariants']

type CodexTrace = {
  started: boolean
  delegated: boolean
  terminal: boolean
  final: boolean
}

function dataOf(event: SessionEvent): Record<string, unknown> {
  return event.data !== null && typeof event.data === 'object' && !Array.isArray(event.data)
    ? event.data as Record<string, unknown>
    : {}
}

function idOf(event: SessionEvent): string {
  const id = dataOf(event).executionId
  if (typeof id !== 'string' || id.length === 0 || id.length > 256) {
    throw new Error(`${event.type} requires a bounded executionId`)
  }
  return id
}

/** Pure replay check used by both the invariant and focused Host tests. */
export function assertCodexLifecycle(events: readonly SessionEvent[]): void {
  const traces = new Map<string, CodexTrace>()
  for (const event of events) {
    if (!event.type.startsWith('codex/')) continue
    const id = idOf(event)
    const trace = traces.get(id) ?? { started: false, delegated: false, terminal: false, final: false }
    if (trace.terminal) throw new Error(`${event.type} appears after terminal for ${id}`)
    switch (event.type) {
      case 'codex/user-start':
        if (trace.started) throw new Error(`duplicate user-start for ${id}`)
        trace.started = true
        break
      case 'codex/delegation-start':
        if (!trace.started || trace.delegated) throw new Error(`delegation-start is not a unique start pair for ${id}`)
        trace.delegated = true
        break
      case 'codex/text-final':
        if (!trace.delegated || trace.final) throw new Error(`text-final is not a unique delegated update for ${id}`)
        trace.final = true
        break
      case 'codex/text-delta':
      case 'codex/tool-status':
      case 'codex/approval-request':
      case 'codex/approval-decision':
      case 'codex/interrupt-intent':
        if (!trace.delegated) throw new Error(`${event.type} precedes delegation-start for ${id}`)
        break
      case 'codex/terminal':
        if (!trace.delegated) throw new Error(`terminal precedes delegation-start for ${id}`)
        trace.terminal = true
        break
      default:
        throw new Error(`unknown Codex lifecycle event ${event.type}`)
    }
    traces.set(id, trace)
  }
}

const install: InvariantInstaller = Object.assign((ctx: Context, fail: InvariantFailure) => {
  const validate = (session: Session, candidate?: SessionEvent): void => {
    try {
      assertCodexLifecycle(candidate === undefined ? session.events : [...session.events, candidate])
    } catch (error) {
      fail(error instanceof Error ? error.message : 'invalid Codex lifecycle')
    }
  }
  for (const session of ctx.sessions.list()) validate(session)
  // HMR/session-fork creation is not replayed through internal/dispatch.  A
  // global creation listener keeps the seed gate in place for newly entered
  // sessions as well as the adoption sweep above.
  ctx.on('session/created', (session) => { validate(session) }, { global: true })
  // Session events are dispatched through the global bus; without the
  // `global` option this package only observes the local context and a
  // second session can bypass the lifecycle gate entirely.
  ctx.on('internal/dispatch', (_mode, eventName, args) => {
    if (eventName !== 'session/event') return
    const [session, event] = args as [Session, SessionEvent]
    validate(session, event)
  }, { global: true })
}, { inject: ['sessions'] })

export const apply = (ctx: Context): Promise<() => void> =>
  Promise.resolve(ctx.invariants.register(PACKAGE_NAME, install))
