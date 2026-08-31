/**
 * Host-owned Codex delegation coordinator.
 *
 * This module is intentionally independent of Cordis and the real bridge
 * socket.  It is the executable ordering/invariant seam: tests inject a fake
 * Agent, durable Session and transport, while the Typert service supplies the
 * live objects in production.
 */

import { randomUUID } from 'node:crypto'
import {
  parseCodexApprovalId,
  parseCodexExecutionId,
  parseCodexSessionId,
} from '../types.ts'
import type {
  CodexApprovalDecision,
  CodexBridgeEvent,
  CodexBridgeExecution,
  CodexBridgeTransport,
  CodexCharacter,
  CodexIsolationOutcome,
  CodexDurableSession,
  CodexApprovalId,
  CodexExecutionId,
  CodexMaintenanceAgent,
  CodexSafeErrorCode,
  CodexStartRequest,
  CodexTerminalStatus,
} from '../types.ts'
import { CODEX_SESSION_EVENT_TYPES } from '../session-events.ts'

export interface CodexCoordinatorOptions {
  readonly flush: (session: CodexDurableSession) => Promise<boolean | void>
  readonly id?: () => string
  readonly interruptTimeoutMs?: number
}

interface Deferred<T> {
  readonly promise: Promise<T>
  resolve(value: T): void
  reject(error: unknown): void
  settled: boolean
}

function deferred<T>(): Deferred<T> {
  let resolvePromise!: (value: T) => void
  let rejectPromise!: (error: unknown) => void
  const promise = new Promise<T>((resolve, reject) => {
    resolvePromise = resolve
    rejectPromise = reject
  })
  // Deferreds are lifecycle signals and some branches intentionally have no
  // waiter (for example a reservation that is canceled before dispatch).
  // Keep rejection observable to explicit awaiters without leaking an
  // unhandled-rejection turn to Node.
  void promise.catch(() => undefined)
  return {
    promise,
    settled: false,
    resolve(value) {
      if (this.settled) return
      this.settled = true
      resolvePromise(value)
    },
    reject(error) {
      if (this.settled) return
      this.settled = true
      rejectPromise(error)
    },
  }
}

interface ExecutionState {
  readonly executionId: CodexExecutionId
  readonly agent: CodexMaintenanceAgent
  readonly session: CodexDurableSession
  readonly request: CodexStartRequest
  readonly character: CodexCharacter
  readonly accepted: Deferred<{ executionId: CodexExecutionId }>
  readonly terminal: Deferred<{ status: CodexTerminalStatus; text: string }>
  /** Resolves after the user/delegation boundary is durably flushed. */
  readonly startDurable: Deferred<boolean>
  readonly reservation: Deferred<CodexBridgeExecution | undefined>
  readonly eventFailure: Deferred<unknown>
  /** Held while a poisoned execution quarantines the DSH Agent. */
  readonly quarantine: Deferred<void>
  readonly approvals: Map<CodexApprovalId, { readonly kind: 'command' | 'file_change' | 'unknown'; readonly safeSummary: string; decided: boolean }>
  maintenance?: Promise<void>
  bridge: CodexBridgeExecution | undefined
  terminalWritten: boolean
  terminalAppended: boolean
  finalAppended: boolean
  terminalWrite: Promise<void> | undefined
  terminalFailure: unknown
  maintenanceSettled: boolean
  /** Durable boundary failed; keep the execution/session poisoned. */
  blocked: boolean
  durableStartWritten: boolean
  interruptIntentWritten: boolean
  agentCancelSent: boolean
  cancelRequested: boolean
  interruptPromise: Promise<void> | undefined
  finalText: string
  sequence: number
  eventChain: Promise<void>
  eventCount: number
  toolCount: number
  approvalCount: number
  visibleChars: number
}

type InterruptReason = 'user' | 'barge-in' | 'mode-switch' | 'shutdown' | 'restart'

/** DSH's Agent.cancel takes a structured cause; never pass UI reason strings. */
function cancelAgent(agent: CodexMaintenanceAgent, reason: InterruptReason): void {
  const cause = reason === 'user'
    ? { kind: 'user' as const }
    : { kind: 'hook' as const, reason: `codex:${reason}` }
  agent.cancel(cause, { keepInbox: true })
}

const MAX_BRIDGE_EVENTS = 4_096
const MAX_TOOL_EVENTS = 512
const MAX_APPROVALS = 64
const MAX_VISIBLE_CHARS = 16_000

interface AgentGuardRecord {
  readonly blockedSessions: Set<string>
  readonly dispose: () => void
}

interface RecoveryPreflight {
  /** A malformed or overlapping tail cannot be repaired without isolation. */
  readonly invalid: boolean
  /** At least one started execution has no authoritative terminal yet. */
  readonly open: boolean
}

/**
 * Synchronous startup admission check.  This deliberately does not attempt
 * isolation or append anything: it runs before `runMaintenance` so a busy
 * Agent's next native pre-step is already rejected while recovery waits for
 * the maintenance slot.  The full, authoritative fold remains `recover()`.
 */
function inspectRecoveryTail(session: CodexDurableSession): RecoveryPreflight {
  const starts = new Map<string, { delegated: boolean; terminal: boolean }>()
  let invalid = false
  for (const event of session.events) {
    if (typeof event.type !== 'string') {
      invalid = true
      break
    }
    if (!event.type.startsWith('codex/')) continue
    if (!(CODEX_SESSION_EVENT_TYPES as readonly string[]).includes(event.type)) {
      invalid = true
      break
    }
    const data = recordOf(event.data)
    const executionId = boundedString(data?.['executionId'], 256)
    if (data === undefined || executionId === undefined) {
      invalid = true
      break
    }
    const trace = starts.get(executionId) ?? { delegated: false, terminal: false }
    if (trace.terminal) {
      invalid = true
      break
    }
    switch (event.type) {
      case 'codex/user-start':
        if (starts.has(executionId) || typeof data['text'] !== 'string'
          || data['text'].length === 0 || data['text'].length > 16_000
          || (data['character'] !== 'default' && data['character'] !== 'xiaoman')) invalid = true
        else starts.set(executionId, trace)
        break
      case 'codex/delegation-start':
        if (!starts.has(executionId) || trace.delegated || data['sessionId'] !== String(session.id)
          || (data['character'] !== 'default' && data['character'] !== 'xiaoman')) invalid = true
        else trace.delegated = true
        break
      case 'codex/terminal':
        if (!starts.has(executionId) || !trace.delegated
          || !['completed', 'interrupted', 'failed'].includes(String(data['status']))) invalid = true
        else trace.terminal = true
        break
      default:
        // Updates are only meaningful after the durable delegation boundary.
        if (!starts.has(executionId) || !trace.delegated) invalid = true
        break
    }
    if (invalid) break
  }
  return { invalid, open: !invalid && [...starts.values()].some(trace => !trace.terminal) }
}

// Keep the authoritative latch on the Agent object itself as well as in the
// current module cache. A Host HMR/reload creates a new WeakMap and
// coordinator, but must not turn an already-poisoned Agent back into an
// admitted Codex turn. The Agent scope owns disposal of the listener.
const AGENT_GUARD_SYMBOL = Symbol.for('deepseek.dsh.codex.agent-guard')
const agentGuardRecords = new WeakMap<object, AgentGuardRecord>()

function agentGuardOf(agent: CodexMaintenanceAgent): AgentGuardRecord | undefined {
  const target = agent as unknown as object
  const cached = agentGuardRecords.get(target)
  if (cached !== undefined) return cached
  const persisted = (target as Record<PropertyKey, unknown>)[AGENT_GUARD_SYMBOL]
  if (persisted !== null && typeof persisted === 'object') {
    const record = persisted as AgentGuardRecord
    agentGuardRecords.set(target, record)
    return record
  }
  return undefined
}

function safeCode(value: unknown): CodexSafeErrorCode {
  const values: readonly CodexSafeErrorCode[] = [
    'not_authenticated', 'bridge_unavailable', 'bridge_protocol',
    'turn_in_progress', 'turn_failed', 'interrupt_timeout', 'invalid_request',
    'approval_unavailable', 'host_restart', 'interrupt_isolated', 'internal_error',
    'isolation_failed', 'mapping_commit_failed', 'security_isolation_unavailable',
  ]
  return typeof value === 'string' && values.includes(value as CodexSafeErrorCode)
    ? value as CodexSafeErrorCode
    : 'internal_error'
}

function safeError(code: CodexSafeErrorCode, message: string): Error & { code: CodexSafeErrorCode } {
  const error = new Error(message) as Error & { code: CodexSafeErrorCode }
  error.code = code
  return error
}

function recordOf(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : undefined
}

function boundedString(value: unknown, maximum: number): string | undefined {
  return typeof value === 'string' && value.length > 0 && value.length <= maximum ? value : undefined
}

/**
 * Append-only lifecycle owner.  A state remains active until the authoritative
 * terminal has been durably flushed and `runMaintenance` has released the
 * Agent, so a second start cannot race a still-owned maintenance phase.
 */
export class CodexCoordinator {
  private readonly bridge: CodexBridgeTransport
  private readonly flush: CodexCoordinatorOptions['flush']
  private readonly makeId: () => string
  private readonly interruptTimeoutMs: number
  private readonly active = new Map<string, ExecutionState>()
  private readonly executions = new Map<string, ExecutionState>()
  private readonly isolatedSessions = new Set<string>()
  /** Crash-tail recovery holds for sessions discovered at service startup. */
  private readonly recoveryHolds = new Map<string, Deferred<void>>()
  /** Agent-scoped pre-step guards survive Host service HMR/unload. */

  constructor(bridge: CodexBridgeTransport, options: CodexCoordinatorOptions) {
    this.bridge = bridge
    this.flush = options.flush
    this.makeId = options.id ?? (() => `codex-${randomUUID()}`)
    this.interruptTimeoutMs = Math.max(1, options.interruptTimeoutMs ?? 2_000)
  }

  /**
   * Recover crash tails before a new execution is claimed.  The synthetic
   * terminal is host-owned and is flushed before the next accepted start.
   */
  async recover(session: CodexDurableSession): Promise<void> {
    type RecoveryTrace = {
      user: boolean
      delegated: boolean
      terminal: boolean
      final: boolean
      sequence: number
      approvals: Set<string>
      decidedApprovals: Set<string>
    }
    const traces = new Map<string, RecoveryTrace>()
    let invalid = false
    for (const event of session.events) {
      if (typeof event.type !== 'string') {
        invalid = true
        break
      }
      if (!event.type.startsWith('codex/')) continue
      const data = recordOf(event.data)
      const id = boundedString(data?.['executionId'], 256)
      if (data === undefined || id === undefined) {
        invalid = true
        break
      }
      const trace = traces.get(id) ?? {
        user: false,
        delegated: false,
        terminal: false,
        final: false,
        sequence: 0,
        approvals: new Set<string>(),
        decidedApprovals: new Set<string>(),
      }
      if (trace.terminal) {
        invalid = true
        break
      }
      const characterValid = data['character'] === 'default' || data['character'] === 'xiaoman'
      const textValid = typeof data['text'] === 'string' && data['text'].length <= 16_000
      switch (event.type) {
        case 'codex/user-start':
          if (trace.user || !textValid || !characterValid) invalid = true
          else trace.user = true
          break
        case 'codex/delegation-start':
          if (!trace.user || trace.delegated || data['sessionId'] !== String(session.id) || !characterValid) invalid = true
          else trace.delegated = true
          break
        case 'codex/text-delta': {
          const sequence = data['sequence']
          if (!trace.delegated || typeof data['text'] !== 'string' || data['text'].length > 16_000
            || (data['phase'] !== 'commentary' && data['phase'] !== 'final_answer')
            || typeof data['speakable'] !== 'boolean'
            || !Number.isSafeInteger(sequence) || sequence !== trace.sequence + 1) invalid = true
          else trace.sequence = sequence
          break
        }
        case 'codex/text-final':
          if (!trace.delegated || trace.final || !textValid) invalid = true
          else trace.final = true
          break
        case 'codex/tool-status':
          if (!trace.delegated || boundedString(data['activityId'], 256) === undefined
            || boundedString(data['activity'], 256) === undefined
            || !['started', 'progress', 'completed', 'denied', 'failed'].includes(String(data['status']))) invalid = true
          break
        case 'codex/approval-request':
          if (!trace.delegated || boundedString(data['approvalId'], 256) === undefined
            || trace.approvals.has(String(data['approvalId']))) invalid = true
          else trace.approvals.add(String(data['approvalId']))
          break
        case 'codex/approval-decision': {
          const approvalId = boundedString(data['approvalId'], 256)
          if (!trace.delegated || approvalId === undefined || !trace.approvals.has(approvalId)
            || trace.decidedApprovals.has(approvalId)
            || !['accept', 'decline', 'cancel'].includes(String(data['decision']))) invalid = true
          else trace.decidedApprovals.add(approvalId)
          break
        }
        case 'codex/interrupt-intent':
          if (!trace.delegated || !['user', 'barge-in', 'mode-switch', 'shutdown', 'restart'].includes(String(data['reason']))) invalid = true
          break
        case 'codex/terminal':
          if (!trace.delegated || trace.terminal
            || !['completed', 'interrupted', 'failed'].includes(String(data['status']))
            || (data['text'] !== undefined && (!textValid || data['status'] !== 'completed'))) invalid = true
          else trace.terminal = true
          break
        default:
          invalid = true
      }
      if (invalid) break
      traces.set(id, trace)
    }
    const openExecutions = [...traces]
      .filter(([, trace]) => trace.user && !trace.terminal)
      .map(([executionId]) => executionId)
    if (!invalid && [...traces.values()].some(trace => trace.user && !trace.delegated)) invalid = true
    if (invalid) {
      // A malformed/overlapping durable tail cannot be repaired by treating a
      // forged terminal as closed. Isolate any known live execution first,
      // then keep this session blocked for operator reconciliation.
      this.isolatedSessions.add(String(session.id))
      const sessionId = parseCodexSessionId(session.id)
      const executionId = parseCodexExecutionId(openExecutions[0])
      if (this.bridge.isolateExecution !== undefined && sessionId !== undefined && executionId !== undefined) {
        try {
          const outcome = await this.bridge.isolateExecution(sessionId, executionId)
          if (outcome !== 'isolated') throw safeError('isolation_failed', 'Codex recovery has no authoritative terminal')
        } catch {
          throw safeError('isolation_failed', 'Codex recovery isolation failed')
        }
      }
      throw safeError('host_restart', 'Codex durable session recovery is invalid')
    }
    if (openExecutions.length === 0) return
    // The backend App Server is process-shared: one exact isolation ACK kills
    // every stale rollout in that process. Do not issue a second exact kill
    // for each durable tail after the first has already retired the mapping.
    if (this.bridge.isolateExecution === undefined) {
      this.isolatedSessions.add(String(session.id))
      throw safeError('isolation_failed', 'Codex restart isolation is unavailable')
    }
    try {
      const sessionId = parseCodexSessionId(session.id)
      const executionId = parseCodexExecutionId(openExecutions[0])
      if (sessionId === undefined || executionId === undefined) {
        this.isolatedSessions.add(String(session.id))
        throw safeError('host_restart', 'Codex durable session recovery is invalid')
      }
      const outcome = await this.bridge.isolateExecution(sessionId, executionId)
      if (outcome !== 'isolated') throw safeError('isolation_failed', 'Codex recovery has no authoritative terminal')
    } catch {
      this.isolatedSessions.add(String(session.id))
      throw safeError('isolation_failed', 'Codex restart isolation failed')
    }
    let changed = false
    for (const executionId of openExecutions) {
      session.append('codex/terminal', {
        executionId,
        status: 'interrupted',
        reason: 'host_restart',
        errorCode: 'host_restart',
      })
      changed = true
    }
    if (changed) {
      try {
        await this.flushDurable(session)
      } catch (error) {
        // Recovery cannot leave an in-memory host_restart marker that looks
        // durable on the next boot.  Poison this session until reconciliation.
        this.isolatedSessions.add(String(session.id))
        throw error
      }
    }
  }

  /**
   * Install the Agent-owned gate before trying to claim recovery maintenance.
   * A busy Agent may reject `runMaintenance`; the gate must nevertheless be
   * live synchronously so a queued native prompt cannot wake in that gap.
   * Returns whether the service should make a maintenance recovery attempt.
   */
  prepareAgentRecovery(agent: CodexMaintenanceAgent): boolean {
    this.ensureAgentGuard(agent)
    const sessionId = String(agent.session.id)
    const result = inspectRecoveryTail(agent.session)
    if (!result.invalid && !result.open) return false
    this.isolatedSessions.add(sessionId)
    this.poisonAgent(agent, sessionId)
    return true
  }

  /**
   * Clear a startup gate only after `recover()` has completed its exact
   * backend isolation and durable reconciliation.  There is intentionally no
   * public unpoison operation for ordinary turn failures.
   */
  reconcileAgentRecovery(agent: CodexMaintenanceAgent): void {
    const sessionId = String(agent.session.id)
    this.isolatedSessions.delete(sessionId)
    agentGuardOf(agent)?.blockedSessions.delete(sessionId)
    const hold = this.recoveryHolds.get(sessionId)
    if (hold !== undefined) {
      hold.resolve()
      this.recoveryHolds.delete(sessionId)
    }
  }

  /**
   * Keep a startup recovery maintenance claim quarantined after isolation or
   * durable recovery failure. The only release is the Agent's dispose signal
   * (or coordinator close); a normal native prompt cannot wake beside the
   * unreconciled durable tail.
   */
  async holdRecovery(sessionId: string, signal: AbortSignal, agent?: CodexMaintenanceAgent): Promise<void> {
    if (agent !== undefined) {
      this.ensureAgentGuard(agent)
      // `recover()` can only mark the durable session as isolated because it
      // is intentionally session-only. Once the live Agent is available,
      // materialize the same persistent pre-step latch used by executions;
      // otherwise a queued native wake could run after the recovery callback
      // returns to idle.
      this.poisonAgent(agent, sessionId)
    }
    if (!this.isolatedSessions.has(sessionId)) return
    let hold = this.recoveryHolds.get(sessionId)
    if (hold === undefined) {
      hold = deferred<void>()
      this.recoveryHolds.set(sessionId, hold)
    }
    // The Agent-scope pre-step guard is the durable fail-closed latch. Do not
    // await the maintenance AbortSignal here: a user/hook cancellation is
    // first-cause-wins and Agent.dispose waits for maintenance to return.
    // Keeping the deferred only gives coordinator.close an explicit owner for
    // recovery cleanup; native wake is rejected by the guard after return.
    if (signal.aborted && agent === undefined) hold.resolve()
  }

  /** Start only after durable acceptance and bridge reservation acknowledgement. */
  async start(agent: CodexMaintenanceAgent, request: CodexStartRequest, signal: AbortSignal): Promise<{ executionId: CodexExecutionId }> {
    const input = recordOf(request)
    const rawText = boundedString(input?.['text'], 16_000)
    if (rawText === undefined || rawText.trim().length === 0) throw safeError('invalid_request', 'Codex request is invalid')
    const rawCharacter = input?.['character']
    if (rawCharacter !== undefined && rawCharacter !== 'default' && rawCharacter !== 'xiaoman') {
      throw safeError('invalid_request', 'Codex character is invalid')
    }
    const rawModel = input?.['model']
    const rawEffort = input?.['reasoningEffort']
    const rawTier = input?.['serviceTier']
    if (rawModel !== undefined && boundedString(rawModel, 128) === undefined) {
      throw safeError('invalid_request', 'Codex model is invalid')
    }
    if (rawEffort !== undefined && boundedString(rawEffort, 32) === undefined) {
      throw safeError('invalid_request', 'Codex reasoning effort is invalid')
    }
    if (rawTier !== undefined && rawTier !== null && boundedString(rawTier, 64) === undefined) {
      throw safeError('invalid_request', 'Codex service tier is invalid')
    }
    const model = typeof rawModel === 'string' ? rawModel : 'gpt-5.4-mini'
    const reasoningEffort = typeof rawEffort === 'string' ? rawEffort : 'low'
    const serviceTier = typeof rawTier === 'string' ? rawTier : null
    const text = rawText.trim()
    const session = agent.session
    const sessionId = String(session.id)
    this.ensureAgentGuard(agent)
    if (this.isolatedSessions.has(sessionId) || this.isAgentBlocked(agent, sessionId)) {
      this.isolatedSessions.add(sessionId)
      throw safeError('isolation_failed', 'Codex session isolation is unavailable')
    }
    if (this.active.has(sessionId)) throw safeError('turn_in_progress', 'A Codex execution is already active')

    const executionId = parseCodexExecutionId(this.makeId())
    if (executionId === undefined) throw safeError('internal_error', 'Codex execution id is invalid')
    const state: ExecutionState = {
      executionId,
      agent,
      session,
      request: {
        text,
        character: rawCharacter === 'xiaoman' ? 'xiaoman' : 'default',
        model,
        reasoningEffort,
        serviceTier,
      },
      character: rawCharacter === 'xiaoman' ? 'xiaoman' : 'default',
      accepted: deferred(),
      terminal: deferred(),
      startDurable: deferred<boolean>(),
      reservation: deferred(),
      eventFailure: deferred<unknown>(),
      quarantine: deferred<void>(),
      approvals: new Map(),
      bridge: undefined,
      terminalWritten: false,
      terminalAppended: false,
      finalAppended: false,
      terminalWrite: undefined,
      terminalFailure: undefined,
      maintenanceSettled: false,
      blocked: false,
      durableStartWritten: false,
      interruptIntentWritten: false,
      agentCancelSent: false,
      cancelRequested: signal.aborted,
      interruptPromise: undefined,
      finalText: '',
      sequence: 0,
      eventChain: Promise.resolve(),
      eventCount: 0,
      toolCount: 0,
      approvalCount: 0,
      visibleChars: 0,
    }
    this.active.set(sessionId, state)
    this.executions.set(state.executionId, state)
    const cleanup = () => {
      // A poisoned state is a durable/reconciliation marker, not a normal
      // completed turn. Keep it visible in the Host status and ownership maps
      // until an explicit coordinator close (or future reconciliation) clears
      // it; otherwise a failed flush would look like a clean idle session and
      // the UI could not distinguish a blocked tail from a reusable turn.
      if (!state.blocked) {
        if (this.active.get(sessionId) === state) this.active.delete(sessionId)
        this.executions.delete(state.executionId)
      }
    }

    const onAbort = () => {
      // Do not abort the reservation transport: a pre-ready interrupt must
      // remain armed until the exact execution/thread/turn identity exists.
      state.cancelRequested = true
      if (state.bridge !== undefined) {
        void this.requestInterrupt(state, 'barge-in').catch(() => undefined)
      }
    }
    if (!signal.aborted) signal.addEventListener('abort', onAbort, { once: true })

    let maintenance: Promise<void>
    try {
      maintenance = agent.runMaintenance(async (maintenanceSignal) => {
        // The maintenance signal is an intent channel, not the reservation
        // signal.  This keeps the exact bridge interrupt race-safe.
        if (maintenanceSignal.aborted) state.cancelRequested = true
        const onMaintenanceAbort = () => {
          state.cancelRequested = true
          if (state.bridge !== undefined && state.durableStartWritten && state.interruptPromise === undefined) {
            void this.requestInterrupt(state, 'barge-in').catch(() => undefined)
          }
        }
        // This listener is only an interrupt-intent channel. Poison release is
        // owned by the Agent-scope pre-step latch below, never by an AbortSignal
        // reason (which is first-cause-wins in ReactLoopAgent).
        maintenanceSignal.addEventListener('abort', onMaintenanceAbort)
        try {
          // Crash-tail recovery is itself a native-Agent maintenance phase;
          // never append recovery markers beside a concurrently running
          // session.prompt loop.
          await this.recover(session)
          session.append('codex/user-start', {
            executionId: state.executionId,
            text,
            character: state.character,
          })
          session.append('codex/delegation-start', {
            executionId: state.executionId,
            sessionId,
            character: state.character,
          })
          await this.flushDurable(session, state)
          state.durableStartWritten = true
          state.startDurable.resolve(true)
          if (state.cancelRequested) {
            // Abort-before-dispatch: durable intent and Agent.cancel happen
            // before a bridge reservation is ever created, so no ghost turn
            // can be spawned after the client has canceled.
            await this.appendInterruptIntent(state, 'barge-in')
            this.cancelAgentOnce(state, 'barge-in')
            state.reservation.resolve(undefined)
            await this.writeTerminal(state, 'interrupted', 'canceled_before_dispatch')
            state.accepted.reject(safeError('invalid_request', 'Codex request was canceled'))
            return
          }
          if (state.blocked) {
            state.reservation.resolve(undefined)
            state.accepted.reject(safeError('isolation_failed', 'Codex session is blocked'))
            return
          }
          const bridgeExecution = await this.bridge.reserve({
            executionId: state.executionId,
            sessionId,
            text,
            character: state.character,
            model: state.request.model ?? 'gpt-5.4-mini',
            reasoningEffort: state.request.reasoningEffort ?? 'low',
            serviceTier: state.request.serviceTier ?? null,
            signal: new AbortController().signal,
          }, event => {
            state.eventChain = state.eventChain
              .then(() => state.eventFailure.settled ? undefined : this.onBridgeEvent(state, event))
              .catch(error => {
                state.eventFailure.resolve(error)
                return undefined
              })
          })
          state.bridge = bridgeExecution
          state.reservation.resolve(bridgeExecution)
          // Accepted means bridge reservation is complete, not merely that a
          // WS request was queued.  This is the ghost-turn race gate.
          state.accepted.resolve({ executionId: state.executionId })
          // An early Remote abort may already own the interrupt promise and
          // be waiting on this exact reservation. Avoid self-awaiting it.
          if (state.cancelRequested && state.interruptPromise === undefined) {
            await this.requestInterrupt(state, 'barge-in')
          }
          // A failed delta/tool flush rejects eventChain before the bridge can
          // emit a terminal. Do not wait forever for an authoritative event
          // after the durable boundary has already failed.
          await Promise.race([
            state.terminal.promise,
            state.eventFailure.promise.then(error => { throw error }),
          ])
        } catch (error: unknown) {
          state.reservation.resolve(undefined)
          if (!state.accepted.settled && state.durableStartWritten) {
            await this.failClosed(state, error, 'bridge_reserve_failed')
            state.accepted.reject(safeError('bridge_unavailable', 'Codex bridge unavailable'))
          } else if (!state.accepted.settled) {
            // This includes recovery/start flush failure.  The in-memory
            // start pair is now an uncheckpointed tail, so the session must
            // remain blocked even if no bridge process was reserved.
            this.poison(state)
            state.startDurable.resolve(false)
            if (state.blocked) {
              state.accepted.reject(safeError('isolation_failed', 'Codex session is blocked'))
              return
            }
            state.accepted.reject(safeError('internal_error', 'Codex host failed to establish durable state'))
          } else if (!state.terminalWritten && state.terminalFailure === undefined) {
            // Once accepted, any parser/flush/transport failure is uncertain
            // until the backend process-facing isolate has acknowledged it.
            await this.failClosed(state, error, 'bridge_failed')
          } else if (state.terminalFailure !== undefined) {
            await this.isolateOrBlock(state)
          }
        } finally {
          signal.removeEventListener('abort', onAbort)
          if (state.terminalFailure !== undefined) await this.isolateOrBlock(state)
          if (!state.blocked && !state.eventFailure.settled && !state.terminalWritten && state.terminalFailure === undefined && state.accepted.settled && state.durableStartWritten) {
            await this.writeTerminal(state, 'failed', 'maintenance_ended', 'internal_error').catch(() => undefined)
          }
          // A poisoned state is now latched on the Agent's own pre-step
          // waterfall. Let ReactLoopAgent return to idle so dispose/HMR can
          // complete; the guard rejects every subsequent native wake until an
          // explicit reconciliation clears the latch.
          maintenanceSignal.removeEventListener('abort', onMaintenanceAbort)
          state.maintenanceSettled = true
          cleanup()
        }
      })
      state.maintenance = maintenance
    } catch {
      cleanup()
      state.startDurable.resolve(false)
      signal.removeEventListener('abort', onAbort)
      throw safeError('turn_in_progress', 'The DSH Agent is busy')
    }
    // Preserve the maintenance promise for error reporting, but never await it
    // here: the Remote returns at the reserve acknowledgement boundary.
    void maintenance.catch((error: unknown) => {
      if (!state.accepted.settled) state.accepted.reject(state.blocked
        ? safeError('isolation_failed', 'Codex session is blocked')
        : safeError('internal_error', 'Codex host failed to start'))
      else if (state.durableStartWritten && !state.terminalWritten && state.terminalFailure === undefined) {
        void this.failClosed(state, error, 'maintenance_failed')
      } else if (!state.blocked) {
        cleanup()
      }
      void error
    })

    return await state.accepted.promise
  }

  /** Durable interrupt intent, Agent cancel, exact bridge interrupt, terminal wait. */
  async interrupt(agent: CodexMaintenanceAgent, executionId: string, reason: 'user' | 'barge-in' | 'mode-switch' | 'shutdown' = 'barge-in'): Promise<{ executionId: CodexExecutionId; accepted: true }> {
    const state = this.executions.get(executionId)
    if (state === undefined || state.agent !== agent) throw safeError('invalid_request', 'Codex execution not found')
    if (state.blocked) throw safeError('isolation_failed', 'Codex session is blocked')
    if (state.terminalWritten) return { executionId: state.executionId, accepted: true }
    await this.requestInterrupt(state, reason)
    return { executionId: state.executionId, accepted: true }
  }

  /** Typed, one-shot approval gate. */
  async approvalDecision(agent: CodexMaintenanceAgent, executionId: string, approvalId: string, decision: CodexApprovalDecision): Promise<{ executionId: CodexExecutionId; approvalId: CodexApprovalId; decision: CodexApprovalDecision }> {
    if (!['accept', 'decline', 'cancel'].includes(decision)) throw safeError('invalid_request', 'Approval decision is not allowed')
    const state = this.executions.get(executionId)
    if (state === undefined || state.agent !== agent || state.terminalWritten) throw safeError('invalid_request', 'Codex execution not found')
    const parsedApprovalId = parseCodexApprovalId(approvalId)
    if (parsedApprovalId === undefined) throw safeError('invalid_request', 'Approval is invalid')
    const approval = state.approvals.get(parsedApprovalId)
    if (approval === undefined || approval.decided) throw safeError('invalid_request', 'Approval is unknown or already decided')
    if (this.bridge.approvalDecision === undefined || state.bridge === undefined) throw safeError('approval_unavailable', 'Codex approval is read-only')
    await this.bridge.approvalDecision(state.bridge, parsedApprovalId, decision)
    approval.decided = true
    state.session.append('codex/approval-decision', { executionId, approvalId: parsedApprovalId, decision })
          await this.flushDurable(state.session, state)
    return { executionId: state.executionId, approvalId: parsedApprovalId, decision }
  }

  /** Current active views; no bridge or prompt payloads are returned. */
  status(sessionId?: string): readonly { executionId: CodexExecutionId; state: 'starting' | 'running' | 'settling' | 'terminal' | 'blocked' }[] {
    return [...this.active.values()].filter(state => sessionId === undefined || String(state.session.id) === sessionId).map(state => ({
      executionId: state.executionId,
      state: state.blocked
        ? 'blocked'
        : state.bridge === undefined
          ? 'starting'
          : state.terminalWritten
            ? state.maintenanceSettled ? 'terminal' : 'settling'
            : 'running',
    }))
  }

  /** Persistent fail-closed gate used by the DSH pre-step waterfall. */
  isBlocked(sessionId: string): boolean {
    return this.isolatedSessions.has(sessionId)
  }

  /** Check the Agent-owned latch when a fresh coordinator follows HMR. */
  isAgentBlocked(agent: CodexMaintenanceAgent, sessionId = String(agent.session.id)): boolean {
    return agentGuardOf(agent)?.blockedSessions.has(sessionId) ?? false
  }

  /**
   * Attach the durable quarantine gate to the Agent's own scope. It must not
   * be owned by CodexService's fiber: unloading/HMR of the Host service must
   * not remove a fail-closed guard while a poisoned Agent remains alive.
   */
  ensureAgentGuard(agent: CodexMaintenanceAgent): void {
    const target = agent as unknown as object
    if (agentGuardOf(agent) !== undefined) return
    const context = (agent as CodexMaintenanceAgent & {
      readonly ctx?: { on?: (name: string, listener: (payload: { agent?: unknown }, next: () => Promise<unknown>) => Promise<unknown>, options?: { prepend?: boolean }) => () => void }
    }).ctx
    if (context?.on === undefined) return
    const blockedSessions = new Set<string>()
    const dispose = context.on('agent/pre-step', ({ agent: subject }, next) => {
      if (subject === agent && blockedSessions.has(String(agent.session.id))) {
        return Promise.resolve({ kind: 'reject' })
      }
      return next()
    }, { prepend: true })
    const record = { blockedSessions, dispose }
    agentGuardRecords.set(target, record)
    Object.defineProperty(target, AGENT_GUARD_SYMBOL, {
      configurable: false,
      enumerable: false,
      value: record,
      writable: false,
    })
  }

  private poisonAgent(agent: CodexMaintenanceAgent, sessionId: string): void {
    this.ensureAgentGuard(agent)
    const record = agentGuardOf(agent)
    record?.blockedSessions.add(sessionId)
  }

  async close(): Promise<void> {
    const states = [...this.executions.values()]
    await Promise.all(states.map(async state => {
      if (state.blocked) return
      try {
        await this.requestInterrupt(state, 'shutdown')
      } catch {
        if (!state.terminalWritten) {
          try {
            const outcome = state.bridge === undefined ? undefined : await this.bridge.isolate(state.bridge)
            if (outcome === 'isolated') {
              await this.writeTerminal(state, 'failed', 'shutdown', 'interrupt_isolated')
            } else if (outcome === undefined) {
              await this.writeTerminal(state, 'failed', 'shutdown')
            }
          } catch {
            this.poison(state)
            await this.writeTerminal(state, 'failed', 'isolation_failed', 'isolation_failed').catch(() => undefined)
          }
        }
      }
    }))
    // The Agent-owned pre-step guard remains fail-closed across service HMR;
    // explicit coordinator close is the lifecycle owner that clears local
    // execution/recovery bookkeeping and closes the bridge resources.
    const holds: Promise<void>[] = []
    for (const state of states) {
      if (state.blocked) {
        state.quarantine.resolve()
        holds.push(state.quarantine.promise)
      }
    }
    await Promise.all(holds)
    const recoveryHolds = [...this.recoveryHolds.values()]
    for (const hold of recoveryHolds) hold.resolve()
    await Promise.all(recoveryHolds.map(hold => hold.promise))
    await Promise.all(states.map(state => state.maintenance ?? Promise.resolve()).map(promise => promise.catch(() => undefined)))
    if (this.bridge.close !== undefined) await this.bridge.close()
    this.active.clear()
    this.executions.clear()
  }

  private async requestInterrupt(state: ExecutionState, reason: InterruptReason): Promise<void> {
    if (state.interruptPromise !== undefined) return state.interruptPromise
    state.cancelRequested = true
    state.interruptPromise = (async () => {
      if (state.terminalWritten) return
      // The maintenance callback owns the pre-durable ordering boundary. An
      // early abort only arms the flag; it cannot append an intent beside an
      // unflushed user/delegation start.
      if (!state.durableStartWritten) {
        const durable = await state.startDurable.promise
        if (!durable || state.terminalWritten) return
      }
      try {
        await this.appendInterruptIntent(state, reason)
      } catch (error) {
        // A failed durable intent is not permission to continue to a bridge
        // turn.  If an exact execution already exists, isolate it first;
        // otherwise poison the session before reserve can create a ghost.
        await this.isolateOrBlock(state)
        // The maintenance owner may be waiting on terminal/eventFailure. A
        // failed interrupt-intent checkpoint is a local terminal condition;
        // resolve the non-rejecting failure signal so it cannot hold the
        // Agent slot forever while the bridge is already poisoned.
        state.eventFailure.resolve(error)
        throw error
      }
      this.cancelAgentOnce(state, reason)
      const bridge = state.bridge ?? await this.waitReservation(state)
      if (bridge === undefined || state.terminalWritten) return
      state.bridge ??= bridge
      try {
        await this.bridge.interrupt(bridge, reason)
      } catch (error) {
        // A WS send failure leaves the App Server turn uncertain.  The
        // transport's isolate operation must be the only source of an
        // isolated terminal; closing a socket is not proof of process exit.
        try {
          const outcome = await this.bridge.isolate(bridge)
          if (outcome === 'isolated') await this.writeTerminal(state, 'failed', 'isolated', 'interrupt_isolated')
        } catch {
          await this.isolateOrBlock(state)
          state.eventFailure.resolve(error)
          throw safeError('isolation_failed', 'Codex process isolation failed')
        }
        throw error
      }
      await Promise.race([
        state.terminal.promise.then(() => undefined),
        new Promise<never>((_, reject) => setTimeout(() => reject(safeError('interrupt_timeout', 'Codex interrupt timed out')), this.interruptTimeoutMs)),
      ]).catch(async (error: unknown) => {
        if ((error as { code?: unknown })?.code === 'interrupt_timeout') {
          // A transport may implement this as a backend process-facing
          // isolation. Only after that operation returns do we persist the
          // explicit failed/isolated terminal; it is never AgentInterrupted.
          try {
            const outcome = await this.bridge.isolate(bridge)
            if (outcome === 'isolated') await this.writeTerminal(state, 'failed', 'isolated', 'interrupt_isolated')
          } catch {
            await this.isolateOrBlock(state)
            state.eventFailure.resolve(error)
            throw safeError('isolation_failed', 'Codex process isolation failed')
          }
        }
        throw error
      })
    })()
    return state.interruptPromise
  }

  private async waitReservation(state: ExecutionState): Promise<CodexBridgeExecution | undefined> {
    try {
      // The bridge owns a bounded reservation timeout. Do not turn an early
      // cancel into a resolved interrupt promise merely because two seconds
      // elapsed: exact identity must be interrupted once it exists.
      return await state.reservation.promise
    } catch {
      return undefined
    }
  }

  private cancelAgentOnce(state: ExecutionState, reason: InterruptReason): void {
    if (state.agentCancelSent) return
    state.agentCancelSent = true
    cancelAgent(state.agent, reason)
  }

  private async appendInterruptIntent(state: ExecutionState, reason: InterruptReason): Promise<void> {
    if (state.interruptIntentWritten) return
    // Set the fence before awaiting flush so a maintenance-signal callback
    // and an explicit Remote interrupt cannot append two intents in parallel.
    state.interruptIntentWritten = true
    state.session.append('codex/interrupt-intent', { executionId: state.executionId, reason })
    try {
      await this.flushDurable(state.session, state)
    } catch (error) {
      state.interruptIntentWritten = false
      throw error
    }
  }

  private async onBridgeEvent(state: ExecutionState, event: CodexBridgeEvent): Promise<void> {
    if (state.terminalWritten) return
    state.eventCount += 1
    if (!Number.isSafeInteger(state.eventCount) || state.eventCount > MAX_BRIDGE_EVENTS) {
      await this.protocolFailure(state)
      return
    }
    switch (event.type) {
      case 'started':
        // reserve() already returned this exact identity; an extra started
        // notification is intentionally ignored rather than re-keying state.
        return
      case 'text_delta':
        if ((event.phase !== 'final_answer' && event.phase !== 'commentary')
          || typeof event.speakable !== 'boolean'
          || boundedString(event.text, MAX_VISIBLE_CHARS) === undefined) {
          await this.protocolFailure(state)
          return
        }
        state.sequence += 1
        if (!Number.isSafeInteger(state.sequence)) {
          await this.protocolFailure(state)
          return
        }
        if (event.phase === 'final_answer') {
          state.visibleChars += event.text.length
          if (state.visibleChars > MAX_VISIBLE_CHARS) {
            await this.protocolFailure(state)
            return
          }
        }
        if (event.phase === 'final_answer') state.finalText += event.text
        state.session.append('codex/text-delta', {
          executionId: state.executionId,
          phase: event.phase,
          text: event.text,
          speakable: event.speakable,
          sequence: state.sequence,
        })
        await this.flushDurable(state.session, state)
        return
      case 'tool':
        if (boundedString(event.activityId, 256) === undefined
          || boundedString(event.activity, 256) === undefined
          || !['started', 'progress', 'completed', 'denied', 'failed'].includes(event.status)
          || (event.safeSummary !== undefined && typeof event.safeSummary !== 'string' || (event.safeSummary?.length ?? 0) > 512)) {
          await this.protocolFailure(state)
          return
        }
        state.toolCount += 1
        if (state.toolCount > MAX_TOOL_EVENTS) {
          await this.protocolFailure(state)
          return
        }
        state.session.append('codex/tool-status', {
          executionId: state.executionId,
          activityId: event.activityId,
          activity: event.activity,
          status: event.status,
          ...(event.safeSummary === undefined ? {} : { safeSummary: event.safeSummary }),
        })
        await this.flushDurable(state.session, state)
        return
      case 'approval':
        if (boundedString(event.approvalId, 256) === undefined
          || (event.kind !== 'command' && event.kind !== 'file_change' && event.kind !== 'unknown')
          || boundedString(event.safeSummary, 512) === undefined) {
          await this.protocolFailure(state)
          return
        }
        state.approvalCount += 1
        if (state.approvalCount > MAX_APPROVALS) {
          await this.protocolFailure(state)
          return
        }
        if (state.approvals.has(event.approvalId)) return
        state.approvals.set(event.approvalId, { kind: event.kind, safeSummary: event.safeSummary, decided: false })
        state.session.append('codex/approval-request', {
          executionId: state.executionId,
          approvalId: event.approvalId,
          kind: event.kind,
          safeSummary: event.safeSummary,
        })
        await this.flushDurable(state.session, state)
        return
      case 'terminal':
        if ((event.status !== 'completed' && event.status !== 'interrupted' && event.status !== 'failed')
          || (event.finalText !== undefined && event.finalText.length > MAX_VISIBLE_CHARS)
          || (event.finalText !== undefined && state.finalText.length > 0
            && !event.finalText.startsWith(state.finalText))) {
          await this.protocolFailure(state)
          return
        }
        if (event.errorCode === 'isolation_failed' || event.errorCode === 'mapping_commit_failed') this.poison(state)
        await this.writeTerminal(
          state,
          event.status,
          event.errorCode === 'interrupt_isolated' ? 'isolated' : event.status === 'failed' ? 'bridge_failed' : 'bridge_terminal',
          event.errorCode,
          event.finalText,
        )
    }
  }

  private async protocolFailure(state: ExecutionState): Promise<void> {
    // Stop accepting additional bridge events before recording the bounded
    // terminal. A real transport may implement isolate as a process-facing
    // kill/ack; the coordinator never waits for another stream terminal.
    try {
      const outcome = state.bridge === undefined ? undefined : await this.bridge.isolate(state.bridge)
      if (outcome === 'isolated' || outcome === undefined) {
        await this.writeTerminal(state, 'failed', 'bridge_protocol', 'bridge_protocol')
      }
    } catch {
      await this.isolateOrBlock(state)
    }
  }

  /** Process-facing isolate is the only safe response to uncertain failure. */
  private async isolateOrBlock(state: ExecutionState): Promise<CodexIsolationOutcome | undefined> {
    if (state.bridge !== undefined) {
      try {
        return await this.bridge.isolate(state.bridge)
      } catch {
        // Fall through: no authority means this session must remain blocked.
      }
    }
    this.poison(state)
    return undefined
  }

  /** Isolate first; write a terminal only when the durable boundary permits it. */
  private async failClosed(state: ExecutionState, error: unknown, reason: string): Promise<void> {
    if (state.blocked) {
      // A failed durable checkpoint already poisoned the session.  We still
      // need to make one process-facing isolation attempt before releasing
      // the maintenance owner; the poison must not turn into a ghost turn.
      await this.isolateOrBlock(state)
      return
    }
    try {
      const outcome = state.bridge === undefined ? undefined : await this.bridge.isolate(state.bridge)
      if (outcome === 'isolated' || outcome === undefined) {
        await this.writeTerminal(state, 'failed', reason, safeCode((error as { code?: unknown })?.code))
      }
    } catch {
      this.poison(state)
      // A failed flush is intentionally not retried or represented as an
      // authoritative terminal.  The in-memory tail remains for reconciliation.
    }
  }

  private async flushDurable(session: CodexDurableSession, state?: ExecutionState): Promise<void> {
    let durable: boolean | void
    try {
      durable = await this.flush(session)
    } catch (error) {
      if (state !== undefined) this.poison(state)
      throw error
    }
    // SessionStore.flush() returns false when no persistence listener accepted
    // the checkpoint. Treat that as a hard boundary failure: no Remote
    // acceptance, interrupt intent, or terminal may be claimed durable then.
    if (durable === false) {
      if (state !== undefined) this.poison(state)
      throw safeError('internal_error', 'Codex durable session is unavailable')
    }
  }

  private poison(state: ExecutionState): void {
    state.blocked = true
    this.isolatedSessions.add(String(state.session.id))
    this.poisonAgent(state.agent, String(state.session.id))
  }

  private async writeTerminal(state: ExecutionState, status: CodexTerminalStatus, reason: string, errorCode?: CodexSafeErrorCode, finalText?: string): Promise<void> {
    if (state.terminalWritten) return
    if (state.terminalWrite !== undefined) return state.terminalWrite
    state.terminalWrite = (async () => {
      // Partial answer deltas are not a durable answer when a turn is
      // interrupted or fails. Persist text-final only for an authoritative
      // completed terminal; otherwise a canceled tail could be replayed or
      // spoken as if it were a finished response.
      const text = status === 'completed' ? (finalText ?? state.finalText) : ''
      if (text.length > 0) {
        state.finalText = text
        if (!state.finalAppended) {
          state.session.append('codex/text-final', { executionId: state.executionId, text })
          state.finalAppended = true
        }
      }
      if (!state.terminalAppended) {
        state.session.append('codex/terminal', {
          executionId: state.executionId,
          status,
          reason,
          ...(errorCode === undefined ? {} : { errorCode }),
          ...(text.length === 0 ? {} : { text }),
        })
        state.terminalAppended = true
      }
      try {
        await this.flushDurable(state.session, state)
      } catch (error: unknown) {
        // The append exists only in memory; do not claim an authoritative
        // terminal, release maintenance, or synthesize AgentInterrupted.
        state.terminalFailure = error
        // A terminal that only exists in the in-memory Session is not an
        // authoritative recovery marker.  Even if process isolation succeeds
        // below, the session must remain blocked for reconciliation.
        this.poison(state)
        state.terminal.reject(error)
        throw error
      }
      state.terminalWritten = true
      state.terminal.resolve({ status, text })
      // Keep both maps until the runMaintenance callback finally returns.
      // The terminal is visible to the client first, but the DSH Agent slot is
      // still owned by this maintenance phase; cleanup happens in that final
      // callback action, preventing the next voice sentence from racing it.
    })()
    try {
      await state.terminalWrite
    } finally {
      state.terminalWrite = undefined
    }
  }
}
