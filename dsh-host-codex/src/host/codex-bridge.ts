  /** Node-side adapter for the Host-only `/api/codex/ws` bridge contract. */

import {
  parseCodexActivityId,
  parseCodexApprovalId,
  parseCodexExecutionId,
  parseCodexSessionId,
  parseCodexThreadId,
  parseCodexTurnId,
} from '../types.ts'
import type {
  CodexBridgeEvent,
  CodexBridgeExecution,
  CodexBridgeTransport,
  CodexCharacter,
  CodexIsolationOutcome,
  CodexSafeErrorCode,
} from '../types.ts'
import { CodexThreadId, CodexTurnId } from '../types.ts'

interface SocketLike {
  readonly readyState: number
  onopen: (() => void) | null
  onmessage: ((event: { data: unknown }) => void) | null
  onerror: (() => void) | null
  onclose: (() => void) | null
  send(value: string): void
  close(): void
}

const OPEN = 1
const MAX_ID_LENGTH = 256
const MAX_TEXT_LENGTH = 16_000
const MAX_ACTIVITY_LENGTH = 256
const MAX_SUMMARY_LENGTH = 512

export interface CodexBridgeTransportOptions {
  readonly readyTimeoutMs?: number
  readonly isolateTimeoutMs?: number
  /** Bound the backend's post-terminal provider cleanup/release fence. */
  readonly releaseTimeoutMs?: number
}

function socketFactory(url: string): SocketLike {
  const ctor = (globalThis as unknown as { WebSocket?: new (url: string) => SocketLike }).WebSocket
  if (typeof ctor !== 'function') throw new Error('Codex host WebSocket is unavailable')
  return new ctor(url)
}

function record(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === 'object' ? value as Record<string, unknown> : undefined
}

function stringValue(value: Record<string, unknown> | undefined, ...keys: string[]): string | undefined {
  if (value === undefined) return undefined
  for (const key of keys) {
    const candidate = value[key]
    if (typeof candidate === 'string' && candidate.length > 0) return candidate
  }
  return undefined
}

function boundedValue(value: string | undefined, maximum: number): string | undefined {
  return value !== undefined && value.length <= maximum ? value : undefined
}

/** Parse the backend isolate ACK without accepting legacy aliases or truthy flags. */
function isolationOutcome(value: Record<string, unknown>): CodexIsolationOutcome | undefined {
  if ('ok' in value && value['ok'] !== true) return undefined
  const status = value['status']
  return status === 'released' || status === 'isolated' ? status : undefined
}

/** Closed bridge vocabulary; do not normalize legacy aliases into events. */
function frameType(value: Record<string, unknown>): string | undefined {
  const raw = value['type']
  if (typeof raw !== 'string') return undefined
  switch (raw) {
    case 'accepted': return 'accepted'
    case 'ready': return 'ready'
    case 'interrupt_requested': return 'interrupt_requested'
    case 'interrupt_result': return 'interrupt_result'
    case 'isolate_result': return 'isolate_result'
    case 'turn/released': return 'released'
    case 'AgentStarted': return 'started'
    case 'AgentTextDelta': return 'text_delta'
    case 'AgentToolActivity': return 'tool'
    case 'AgentApprovalRequest': return 'approval'
    case 'AgentInterrupted': return 'interrupted'
    case 'AgentFinished': return 'finished'
    case 'AgentError': return 'error'
    // The bridge's validation/error control frame is distinct from the
    // AgentError terminal event and is intentionally the only lowercase
    // event accepted here.
    case 'error': return 'error'
    default: return undefined
  }
}

function exactIdentity(pending: Pending, value: Record<string, unknown>, started: boolean): boolean {
  const sessionId = boundedValue(stringValue(value, 'session_id'), MAX_ID_LENGTH)
  const executionId = boundedValue(stringValue(value, 'execution_id'), MAX_ID_LENGTH)
  const threadId = boundedValue(stringValue(value, 'thread_id'), MAX_ID_LENGTH)
  const turnId = boundedValue(stringValue(value, 'turn_id'), MAX_ID_LENGTH)
  if (sessionId !== pending.sessionId || executionId !== pending.executionId || threadId === undefined || turnId === undefined) return false
  if (started) return true
  return pending.execution?.threadId === threadId && pending.execution.turnId === turnId
}

/** `turn/isolate` ACKs are execution-scoped by contract. The backend may
 * echo the exact pair when it still has it, but a process-wide kill can
 * legitimately acknowledge after the WS turn mapping has been retired. */
function exactExecutionIdentity(pending: Pending, value: Record<string, unknown>): boolean {
  const sessionId = boundedValue(stringValue(value, 'session_id'), MAX_ID_LENGTH)
  const executionId = boundedValue(stringValue(value, 'execution_id'), MAX_ID_LENGTH)
  if (sessionId !== pending.sessionId || executionId !== pending.executionId) return false
  const rawThread = stringValue(value, 'thread_id')
  const rawTurn = stringValue(value, 'turn_id')
  if ((rawThread === undefined) !== (rawTurn === undefined)) return false
  if (rawThread === undefined) return true
  const threadId = boundedValue(rawThread, MAX_ID_LENGTH)
  const turnId = boundedValue(rawTurn, MAX_ID_LENGTH)
  return threadId === pending.execution?.threadId && turnId === pending.execution?.turnId
}

/** Closed identity check for the host-only HTTP isolate response. */
function exactHttpIdentity(execution: CodexBridgeExecution, value: Record<string, unknown>): boolean {
  if (value['session_id'] !== execution.sessionId || value['execution_id'] !== execution.executionId) return false
  const hasThread = 'thread_id' in value
  const hasTurn = 'turn_id' in value
  if (hasThread !== hasTurn) return false
  if (!hasThread) return true
  return value['thread_id'] === execution.threadId && value['turn_id'] === execution.turnId
}

function safeErrorCode(value: unknown): CodexSafeErrorCode {
  const allowed: readonly CodexSafeErrorCode[] = [
    'not_authenticated', 'bridge_unavailable', 'bridge_protocol', 'turn_in_progress',
    'turn_failed', 'interrupt_timeout', 'invalid_request', 'approval_unavailable',
    'host_restart', 'interrupt_isolated', 'isolation_failed', 'mapping_commit_failed', 'security_isolation_unavailable', 'internal_error',
  ]
  return typeof value === 'string' && allowed.includes(value as CodexSafeErrorCode)
    ? value as CodexSafeErrorCode
    : 'turn_failed'
}

interface Pending {
  readonly executionId: string
  readonly sessionId: string
  readonly socket: SocketLike
  readonly onEvent: (event: CodexBridgeEvent) => void
  readonly ready: Promise<CodexBridgeExecution>
  execution?: CodexBridgeExecution
  resolveReady(value: CodexBridgeExecution): void
  rejectReady(error: unknown): void
  readySettled: boolean
  /** The turn/start write was attempted; pre-ready failures are uncertain. */
  turnStartSent: boolean
  /** A turn/start write occurred before AgentStarted; isolation is in flight. */
  preReadyIsolation: boolean
  terminal: boolean
  terminalDelivered: boolean
  releaseFence: boolean
  /** Accepted terminal held until backend provider cleanup is released. */
  terminalEvent?: Extract<CodexBridgeEvent, { type: 'terminal' }>
  readyTimer?: ReturnType<typeof setTimeout>
  releaseTimer?: ReturnType<typeof setTimeout>
  isolation?: {
    readonly promise: Promise<CodexIsolationOutcome>
    resolve(outcome: CodexIsolationOutcome): void
    reject(error: unknown): void
    settled: boolean
    ws?: {
      resolve(outcome: CodexIsolationOutcome): void
      reject(error: unknown): void
      timer: ReturnType<typeof setTimeout>
    }
  }
}

/**
 * A deliberately small Host-only transport.  It waits for `AgentStarted` before
 * resolving `reserve`, and every interrupt includes session/execution/thread/
 * turn identifiers.  There is no browser-facing fallback here.
 */
export class WebSocketCodexBridgeTransport implements CodexBridgeTransport {
  private readonly bridgeUrl: string
  private readonly readyTimeoutMs: number
  private readonly isolateTimeoutMs: number
  private readonly releaseTimeoutMs: number
  private readonly sockets = new Set<SocketLike>()
  private readonly pending = new Map<string, Pending>()
  /** Execution ids that crossed the backend release fence normally. */
  private readonly released = new Set<string>()

  constructor(bridgeUrl: string, options: CodexBridgeTransportOptions = {}) {
    this.bridgeUrl = bridgeUrl.replace(/\/$/, '')
    this.readyTimeoutMs = Math.max(1, options.readyTimeoutMs ?? 10_000)
    this.isolateTimeoutMs = Math.max(1, options.isolateTimeoutMs ?? 5_000)
    this.releaseTimeoutMs = Math.max(1, options.releaseTimeoutMs ?? 10_000)
  }

  async reserve(
    request: {
      executionId: string
      sessionId: string
      text: string
      character: CodexCharacter
      model: string
      reasoningEffort: string
      serviceTier: string | null
      signal: AbortSignal
    },
    onEvent: (event: CodexBridgeEvent) => void,
  ): Promise<CodexBridgeExecution> {
    const protocol = this.bridgeUrl.startsWith('https:') ? 'wss:' : 'ws:'
    const socket = socketFactory(`${protocol}//${this.bridgeUrl.replace(/^https?:\/\//, '')}/api/codex/ws?session_id=${encodeURIComponent(request.sessionId)}&character=${encodeURIComponent(request.character)}`)
    this.sockets.add(socket)
    let resolveReady!: (value: CodexBridgeExecution) => void
    let rejectReady!: (error: unknown) => void
    const ready = new Promise<CodexBridgeExecution>((resolve, reject) => {
      resolveReady = resolve
      rejectReady = reject
    })
    const pending: Pending = {
      executionId: request.executionId,
      sessionId: request.sessionId,
      socket,
      onEvent,
      ready,
      resolveReady,
      rejectReady,
      readySettled: false,
      turnStartSent: false,
      preReadyIsolation: false,
      terminal: false,
      terminalDelivered: false,
      releaseFence: false,
    }
    this.pending.set(request.executionId, pending)
    let preReadyIsolationStarted = false
    const failBeforeReady = (message: string) => {
      if (pending.turnStartSent) {
        if (preReadyIsolationStarted) return
        preReadyIsolationStarted = true
        pending.preReadyIsolation = true
        void this.failDispatchedReservation(pending, message)
        return
      }
      if (pending.readySettled) {
        this.failAcceptedTransport(pending, 'bridge_unavailable')
        return
      }
      if (!pending.readySettled) {
        pending.readySettled = true
        if (pending.readyTimer !== undefined) clearTimeout(pending.readyTimer)
        rejectReady(new Error(message))
      }
      if (!pending.terminal) this.emitTerminal(pending, { type: 'terminal', status: 'failed', errorCode: 'bridge_unavailable' })
    }
    socket.onopen = () => {
      if (socket.readyState !== OPEN) return
      try {
        socket.send(JSON.stringify({ type: 'initialize', session_id: request.sessionId }))
        // Once this write is attempted the backend may own a live turn even
        // if the socket closes before AgentStarted.  Treat that window as
        // process-facing uncertainty and require the exact HTTP isolate ACK.
        pending.turnStartSent = true
        socket.send(JSON.stringify({
          type: 'turn/start',
          session_id: request.sessionId,
          correlation_id: request.executionId,
          execution_id: request.executionId,
          text: request.text,
          character: request.character,
          model: request.model,
          reasoning_effort: request.reasoningEffort,
          service_tier: request.serviceTier,
        }))
      } catch {
        failBeforeReady('Codex bridge write failed')
      }
    }
    pending.readyTimer = setTimeout(() => failBeforeReady('Codex bridge reservation timed out'), this.readyTimeoutMs)
    socket.onerror = () => failBeforeReady('Codex bridge connection error')
    socket.onclose = () => {
      this.sockets.delete(socket)
      if (pending.readyTimer !== undefined) clearTimeout(pending.readyTimer)
      if (!pending.readySettled) {
        failBeforeReady('Codex bridge connection closed')
        return
      }
      if (!pending.terminal) {
        // A post-reservation disconnect is uncertain. Do not manufacture a
        // normal failed terminal or release maintenance until process-facing
        // isolation has acknowledged (or has explicitly failed closed).
        this.failAcceptedTransport(pending, 'bridge_unavailable')
        return
      }
      if (pending.isolation !== undefined) {
        // `isolate()` owns the fallback to the host-only HTTP control route;
        // do not reject its WS waiter here before that fallback can run.
      }
      // A terminal without a backend release fence is still process-facing
      // uncertainty. Keep the pending entry and prove isolation through the
      // independent HTTP route instead of releasing maintenance on close.
      this.failAcceptedTransport(pending, 'bridge_unavailable')
    }
    socket.onmessage = event => this.onMessage(pending, event.data)
    request.signal.addEventListener('abort', () => {
      // The coordinator owns the interrupt intent. Closing this socket here
      // would lose the exact identity before the bridge can reserve it.
    }, { once: true })
    try {
      return await ready
    } catch (error) {
      try { socket.close() } catch { /* already closed */ }
      throw error
    }
  }

  /**
   * A turn/start write without AgentStarted is not a local reservation miss:
   * the backend may already have spawned/accepted the turn.  Prove the exact
   * process outcome before rejecting the reservation or exposing a terminal.
   */
  private async failDispatchedReservation(pending: Pending, message: string): Promise<void> {
    if (pending.readySettled) return
    let terminalError: CodexSafeErrorCode
    try {
      const outcome = await this.isolateExecution(pending.sessionId, pending.executionId)
      // A released ACK without an AgentStarted/terminal snapshot cannot be
      // resumed as a normal answer. Keep the downstream durable owner blocked
      // rather than forging a completed/interrupted answer.
      terminalError = outcome === 'isolated' ? 'interrupt_isolated' : 'isolation_failed'
    } catch {
      terminalError = 'isolation_failed'
    }
    // Do not reject reserve until the process-facing outcome is authoritative;
    // otherwise the coordinator can release maintenance while isolation is
    // still in flight and leave a ghost turn behind.
    pending.readySettled = true
    if (pending.readyTimer !== undefined) clearTimeout(pending.readyTimer)
    pending.rejectReady(new Error(message))
    this.emitTerminal(pending, { type: 'terminal', status: 'failed', errorCode: terminalError }, true)
  }

  async interrupt(execution: CodexBridgeExecution, reason: string): Promise<void> {
    const pending = this.pending.get(execution.executionId)
    if (pending === undefined || pending.socket.readyState !== OPEN) throw new Error('Codex bridge execution is not connected')
    pending.socket.send(JSON.stringify({
      type: 'turn/interrupt',
      session_id: execution.sessionId,
      thread_id: execution.threadId,
      turn_id: execution.turnId,
      execution_id: execution.executionId,
      reason,
    }))
  }

  async isolate(execution: CodexBridgeExecution): Promise<CodexIsolationOutcome> {
    const pending = this.pending.get(execution.executionId)
    // A late cleanup race after normal terminal+release is converged. Do not
    // send an isolate request that could kill a later turn on the same server.
    if (pending === undefined) {
      if (this.released.has(execution.executionId)) return 'released'
      throw new Error('Codex bridge execution is not connected')
    }
    if (pending.releaseFence) return 'released'
    if (pending.isolation !== undefined) return pending.isolation.promise
    return this.startIsolationOperation(pending, execution)
  }

  /** Run the complete WS-ACK then HTTP-fallback operation exactly once. */
  private async startIsolationOperation(
    pending: Pending,
    execution: CodexBridgeExecution,
  ): Promise<CodexIsolationOutcome> {
    let resolve!: (outcome: CodexIsolationOutcome) => void
    let reject!: (error: unknown) => void
    const promise = new Promise<CodexIsolationOutcome>((resolvePromise, rejectPromise) => {
      resolve = resolvePromise
      reject = rejectPromise
    })
    void promise.catch(() => undefined)
    const operation: NonNullable<Pending['isolation']> = {
      promise,
      resolve: (outcome: CodexIsolationOutcome) => {
        if (operation.settled) return
        operation.settled = true
        resolve(outcome)
      },
      reject: error => {
        if (operation.settled) return
        operation.settled = true
        reject(error)
      },
      settled: false,
    }
    pending.isolation = operation
    void this.runIsolationOperation(pending, execution, operation).catch(() => undefined)
    return promise
  }

  /** Backend WS ACK and HTTP fallback share this one outer operation. */
  private async runIsolationOperation(
    pending: Pending,
    execution: CodexBridgeExecution,
    operation: NonNullable<Pending['isolation']>,
  ): Promise<void> {
    let outcome: CodexIsolationOutcome | undefined
    try {
      // A normal terminal/release may win before this task gets its first
      // turn.  Do not send a process-wide isolate after the backend has
      // already crossed its cleanup fence; that could kill a later turn on
      // the shared App Server.
      if (pending.releaseFence || this.released.has(execution.executionId)) {
        operation.resolve('released')
        return
      }
      if (pending.socket.readyState === OPEN) {
        let resolveWs!: (outcome: CodexIsolationOutcome) => void
        let rejectWs!: (error: unknown) => void
        const wsPromise = new Promise<CodexIsolationOutcome>((resolvePromise, rejectPromise) => {
          resolveWs = resolvePromise
          rejectWs = rejectPromise
        })
        void wsPromise.catch(() => undefined)
        const ws = {
          resolve: resolveWs,
          reject: rejectWs,
          timer: setTimeout(() => rejectWs(new Error('Codex bridge isolation acknowledgement timed out')), this.isolateTimeoutMs),
        }
        operation.ws = ws
        try {
          if (pending.releaseFence || this.released.has(execution.executionId)) {
            operation.resolve('released')
            return
          }
          pending.socket.send(JSON.stringify({
            type: 'turn/isolate',
            session_id: execution.sessionId,
            thread_id: execution.threadId,
            turn_id: execution.turnId,
            execution_id: execution.executionId,
          }))
          outcome = await wsPromise
        } catch {
          // A closed/broken WS cannot prove process isolation. Fall through to
          // the independent host-only HTTP control operation.
        } finally {
          clearTimeout(ws.timer)
          if (operation.ws === ws) delete operation.ws
        }
      }
      if (outcome === undefined) outcome = await this.isolateViaHttp(execution)
      // A normal terminal/release is monotonic success. Never rewrite the
      // completed answer after the backend has crossed its release fence.
      if (outcome === 'released' || pending.releaseFence || this.released.has(execution.executionId)) {
        if (!this.markReleased(pending)) throw new Error('Codex bridge released without a natural terminal')
        operation.resolve('released')
        return
      }
      const isolatedTerminal: Extract<CodexBridgeEvent, { type: 'terminal' }> = {
        type: 'terminal',
        status: 'failed',
        errorCode: 'interrupt_isolated',
      }
      if (pending.terminal) this.forceTerminal(pending, isolatedTerminal)
      else this.emitTerminal(pending, isolatedTerminal, true)
      operation.resolve('isolated')
    } catch (error) {
      // Release is monotonic.  If the normal terminal/release path won while
      // the fallback request was in flight, its failure is no longer an
      // isolation failure for this execution.
      if (pending.releaseFence || this.released.has(execution.executionId)) {
        operation.resolve('released')
        return
      }
      operation.reject(error)
      throw error
    } finally {
      if (this.pending.get(pending.executionId) === pending && pending.isolation === operation && operation.settled) {
        delete pending.isolation
      }
    }
  }

  /**
   * Recover an execution for which no WS mapping survived a Host restart.
   * The independent Host-only control route is the only valid proof before a
   * synthetic `host_restart` terminal may be appended.
   */
  async isolateExecution(sessionId: string, executionId: string): Promise<CodexIsolationOutcome> {
    return this.isolateViaHttp({
      executionId: parseCodexExecutionId(executionId) ?? (() => { throw new Error('Codex execution id is invalid') })(),
      sessionId: parseCodexSessionId(sessionId) ?? (() => { throw new Error('Codex session id is invalid') })(),
      threadId: CodexThreadId(''),
      turnId: CodexTurnId(''),
    })
  }

  async close(): Promise<void> {
    for (const socket of this.sockets) {
      try { socket.close() } catch { /* already closed */ }
    }
    this.sockets.clear()
    for (const pending of this.pending.values()) {
      if (pending.releaseTimer !== undefined) clearTimeout(pending.releaseTimer)
      if (pending.isolation !== undefined) {
        pending.isolation.ws?.reject(new Error('Codex bridge closed'))
        pending.isolation.reject(new Error('Codex bridge closed'))
      }
    }
    this.pending.clear()
  }

  private onMessage(pending: Pending, raw: unknown): void {
    let value: Record<string, unknown> | undefined
    try { value = record(typeof raw === 'string' ? JSON.parse(raw) : raw) } catch { value = undefined }
    if (value === undefined) {
      if (!pending.readySettled && !pending.turnStartSent) {
        pending.readySettled = true
        if (pending.readyTimer !== undefined) clearTimeout(pending.readyTimer)
        pending.rejectReady(new Error('Codex bridge sent an invalid event'))
      } else {
        this.failAcceptedTransport(pending, 'bridge_protocol')
      }
      return
    }
    const type = frameType(value)
    if (type === undefined) {
      this.failAcceptedTransport(pending, 'bridge_protocol')
      return
    }
    const rawExecutionId = stringValue(value, 'execution_id')
    const executionId = boundedValue(rawExecutionId, MAX_ID_LENGTH)
    if (rawExecutionId !== undefined && executionId === undefined) {
      this.failAcceptedTransport(pending, 'bridge_protocol')
      return
    }
    if (executionId !== undefined && executionId !== pending.executionId) {
      // One WS owns exactly one execution.  A foreign execution id is not a
      // harmless late packet: accepting it would let a stale turn settle a
      // new reservation.  Isolate the process-facing turn before failure.
      this.failAcceptedTransport(pending, 'bridge_protocol')
      return
    }
    if (type === 'accepted') {
      const sessionId = boundedValue(stringValue(value, 'session_id'), MAX_ID_LENGTH)
      const correlationId = boundedValue(stringValue(value, 'correlation_id'), MAX_ID_LENGTH)
      if (sessionId !== pending.sessionId || correlationId !== pending.executionId) {
        this.failAcceptedTransport(pending, 'bridge_protocol')
      }
      return
    }
    if (type === 'ready') {
      const sessionId = boundedValue(stringValue(value, 'session_id'), MAX_ID_LENGTH)
      if (sessionId !== pending.sessionId) this.failAcceptedTransport(pending, 'bridge_protocol')
      return
    }
    if (type === 'interrupt_requested') {
      if (pending.execution === undefined || !exactIdentity(pending, value, false)) {
        this.failAcceptedTransport(pending, 'bridge_protocol')
      }
      return
    }
    if (type === 'interrupt_result') {
      const terminal = record(value['terminal'])
      // Python's interrupt control envelope carries the authoritative
      // identity on its nested Agent* terminal, not on the outer control
      // object. Validate that nested closed shape before replaying it through
      // the normal Agent event parser.
      if (pending.execution === undefined || terminal === undefined || !exactIdentity(pending, terminal, false)) {
        this.failAcceptedTransport(pending, 'bridge_protocol')
        return
      }
      const terminalType = frameType(terminal)
      if (terminalType !== 'interrupted' && terminalType !== 'finished' && terminalType !== 'error') {
        this.failAcceptedTransport(pending, 'bridge_protocol')
        return
      }
      this.onMessage(pending, terminal)
      return
    }
    if (type === 'isolate_result') {
      if (value['ok'] !== true || pending.execution === undefined || !exactExecutionIdentity(pending, value)) {
        this.failAcceptedTransport(pending, 'bridge_protocol')
        return
      }
      const wait = pending.isolation?.ws
      if (wait === undefined) return
      const outcome = isolationOutcome(value)
      if (outcome === 'released') {
        if (!pending.terminal) {
          wait.reject(new Error('Codex bridge released without a natural terminal'))
          return
        }
        this.markReleased(pending)
        clearTimeout(wait.timer)
        wait.resolve('released')
      } else if (outcome === 'isolated') {
        clearTimeout(wait.timer)
        wait.resolve('isolated')
      } else {
        clearTimeout(wait.timer)
        wait.reject(new Error('Codex bridge isolation was not acknowledged'))
      }
      return
    }
    if (type === 'released') {
      if (pending.execution === undefined || !exactIdentity(pending, value, false)) {
        this.failAcceptedTransport(pending, 'bridge_protocol')
        return
      }
      // The backend sends this only after its provider generator has run its
      // cleanup/finally path.  A normal terminal is authoritative, but the
      // release fence is what makes it safe for the Host to retire the WS.
      if (!pending.terminal) {
        this.failAcceptedTransport(pending, 'bridge_protocol')
        return
      }
      this.markReleased(pending)
      return
    }
    // The process-facing isolation operation may race the bridge's verified
    // app-server process terminal. Once isolation is pending, that exact
    // terminal is the authoritative success acknowledgement; do not reject
    // the waiter and let the later isolate_result become a no-op.
    const errorCode = stringValue(value, 'error_code')
    if (pending.execution === undefined) {
      if (type === 'started' && exactIdentity(pending, value, true)) {
        // handled below
      } else {
        this.failAcceptedTransport(pending, 'bridge_protocol')
        return
      }
    } else if (type !== 'started' && !exactIdentity(pending, value, false)) {
      // Every accepted data/terminal/isolate/release frame is bound to the
      // one session/execution/thread/turn tuple. Missing or mixed identity is
      // a protocol failure, never a best-effort match.
      this.failAcceptedTransport(pending, 'bridge_protocol')
      return
    }
    const processExited = type === 'error' && (errorCode === 'process_exit' || errorCode === 'isolation_failed')
    if (processExited && pending.isolation !== undefined) {
      if (errorCode === 'process_exit') {
        pending.isolation.ws?.resolve('isolated')
      } else {
        pending.isolation.ws?.reject(new Error('Codex process isolation failed'))
      }
      return
    }
    if (type === 'started') {
      if (pending.execution !== undefined) {
        this.failAcceptedTransport(pending, 'bridge_protocol')
        return
      }
      const threadId = boundedValue(stringValue(value, 'thread_id'), MAX_ID_LENGTH)
      const turnId = boundedValue(stringValue(value, 'turn_id'), MAX_ID_LENGTH)
      if (threadId === undefined || turnId === undefined) {
        this.failAcceptedTransport(pending, 'bridge_protocol')
        return
      }
      const executionId = parseCodexExecutionId(pending.executionId)
      const sessionId = parseCodexSessionId(pending.sessionId)
      const parsedThreadId = parseCodexThreadId(threadId)
      const parsedTurnId = parseCodexTurnId(turnId)
      if (executionId === undefined || sessionId === undefined || parsedThreadId === undefined || parsedTurnId === undefined) {
        this.failAcceptedTransport(pending, 'bridge_protocol')
        return
      }
      const execution: CodexBridgeExecution = {
        executionId,
        sessionId,
        threadId: parsedThreadId,
        turnId: parsedTurnId,
      }
      pending.execution = execution
      if (!pending.readySettled) {
        pending.readySettled = true
        if (pending.readyTimer !== undefined) clearTimeout(pending.readyTimer)
        pending.resolveReady(execution)
      }
      return
    }
    if (type === 'text_delta') {
      const phase = stringValue(value, 'phase')
      const text = boundedValue(stringValue(value, 'text'), MAX_TEXT_LENGTH)
      if ((phase !== 'final_answer' && phase !== 'commentary') || typeof value['speakable'] !== 'boolean' || text === undefined) {
        this.failAcceptedTransport(pending, 'bridge_protocol')
        return
      }
      pending.onEvent({ type: 'text_delta', phase, text, speakable: value['speakable'] })
      return
    }
    if (type === 'tool' || type === 'approval') {
      const rawActivityId = stringValue(value, 'item_id')
      const rawActivity = stringValue(value, 'activity')
      const activityId = parseCodexActivityId(rawActivityId)
      const activity = boundedValue(rawActivity, MAX_ACTIVITY_LENGTH)
      if (activityId === undefined || activity === undefined) {
        this.failAcceptedTransport(pending, 'bridge_protocol')
        return
      }
      const rawStatus = stringValue(value, 'status')
      const status = rawStatus === 'completed' || rawStatus === 'progress' || rawStatus === 'denied' || rawStatus === 'failed'
        ? rawStatus
        : 'started'
      if (type === 'approval') {
        const approvalId = parseCodexApprovalId(boundedValue(stringValue(value, 'approval_id'), MAX_ID_LENGTH))
        const safeSummary = boundedValue(stringValue(value, 'safe_summary'), MAX_SUMMARY_LENGTH)
        if (approvalId !== undefined && safeSummary !== undefined) {
          pending.onEvent({ type: 'approval', approvalId, kind: activity.includes('file') ? 'file_change' : 'command', safeSummary })
          return
        }
      }
      const rawSafeSummary = stringValue(value, 'safe_summary')
      const safeSummary = boundedValue(rawSafeSummary, MAX_SUMMARY_LENGTH)
      if (rawSafeSummary !== undefined && safeSummary === undefined) {
        this.failAcceptedTransport(pending, 'bridge_protocol')
        return
      }
      pending.onEvent({
        type: 'tool',
        activityId,
        activity,
        status,
        ...(safeSummary === undefined ? {} : { safeSummary }),
      })
      return
    }
    if (type === 'interrupted') {
      if (stringValue(value, 'status') !== 'interrupted') {
        this.failAcceptedTransport(pending, 'bridge_protocol')
        return
      }
      this.emitTerminal(pending, { type: 'terminal', status: 'interrupted' })
      return
    }
    if (type === 'error' || type === 'finished') {
      const status = stringValue(value, 'status')
      if (status === undefined) {
        this.failAcceptedTransport(pending, 'bridge_protocol')
        return
      }
      const terminalStatus: 'completed' | 'interrupted' | 'failed' = status === 'completed' ? 'completed' : status === 'interrupted' ? 'interrupted' : 'failed'
      const rawFinalText = stringValue(value, 'final_text')
      const finalText = boundedValue(rawFinalText, MAX_TEXT_LENGTH)
      if (rawFinalText !== undefined && finalText === undefined) {
        this.failAcceptedTransport(pending, 'bridge_protocol')
        return
      }
      this.emitTerminal(pending, {
        type: 'terminal',
        status: terminalStatus,
        ...(finalText === undefined ? {} : { finalText }),
        ...(terminalStatus === 'failed' ? { errorCode: safeErrorCode(stringValue(value, 'error_code')) } : {}),
      })
      return
    }
    // Every accepted frame must belong to the closed Host bridge vocabulary.
    // Unknown post-ready packets fail closed through process isolation rather
    // than waiting for a reservation/interrupt timeout.
    this.failAcceptedTransport(pending, 'bridge_protocol')
  }

  private emitTerminal(pending: Pending, event: Extract<CodexBridgeEvent, { type: 'terminal' }>, release = false): void {
    // A terminal before AgentStarted is a failed reservation, not an accepted
    // execution. Reject the reserve promise so the Remote cannot hang forever.
    if (!pending.readySettled) {
      pending.readySettled = true
      if (pending.readyTimer !== undefined) clearTimeout(pending.readyTimer)
      pending.rejectReady(new Error('Codex bridge terminated before reservation'))
    }
    // Do not reject an in-flight isolate waiter here. A normal terminal may
    // race an isolate request; it must settle on `turn/released` or the
    // independent HTTP ACK, not fall through to a duplicate kill.
    if (pending.terminal) return
    pending.terminal = true
    pending.terminalEvent = event
    // Accepted turns remain mapped until the backend's `turn/released` fence
    // proves provider cleanup.  If the terminal was pre-reservation, there is
    // no process-facing state to protect and retirement is immediate.
    if (pending.execution === undefined || release) {
      this.deliverTerminal(pending)
      this.retirePending(pending)
    } else {
      this.armReleaseTimer(pending)
    }
  }

  /** Bound a normal terminal's provider cleanup/release fence. */
  private armReleaseTimer(pending: Pending): void {
    if (pending.releaseTimer !== undefined) return
    pending.releaseTimer = setTimeout(() => {
      delete pending.releaseTimer
      void this.isolateAfterReleaseFailure(pending)
    }, this.releaseTimeoutMs)
  }

  /** Isolate before exposing a terminal when `turn/released` never arrives. */
  private async isolateAfterReleaseFailure(pending: Pending): Promise<void> {
    if (this.pending.get(pending.executionId) !== pending || pending.execution === undefined) return
    try {
      const outcome = await this.isolate(pending.execution)
      if (outcome === 'released' || pending.releaseFence || this.released.has(pending.executionId)) return
      this.forceTerminal(pending, { type: 'terminal', status: 'failed', errorCode: 'interrupt_isolated' })
    } catch {
      // The coordinator receives this with release=true and poisons the
      // durable session instead of waiting forever for an impossible fence.
      this.forceTerminal(pending, { type: 'terminal', status: 'failed', errorCode: 'isolation_failed' })
    }
  }

  /** Cross the normal backend release fence and preserve the held terminal. */
  private markReleased(pending: Pending): boolean {
    if (!pending.terminal) return false
    pending.releaseFence = true
    if (pending.isolation !== undefined) {
      pending.isolation.ws?.resolve('released')
      pending.isolation.resolve('released')
    }
    this.deliverTerminal(pending)
    this.retirePending(pending)
    return true
  }

  private forceTerminal(pending: Pending, event: Extract<CodexBridgeEvent, { type: 'terminal' }>): void {
    if (pending.terminalDelivered) return
    if (pending.releaseTimer !== undefined) clearTimeout(pending.releaseTimer)
    pending.terminal = true
    pending.terminalEvent = event
    this.deliverTerminal(pending)
    this.retirePending(pending)
  }

  private deliverTerminal(pending: Pending): void {
    if (pending.terminalDelivered) return
    const event = pending.terminalEvent
    if (event === undefined) return
    pending.terminalDelivered = true
    delete pending.terminalEvent
    pending.onEvent(event)
  }

  private retirePending(pending: Pending): void {
    if (pending.readyTimer !== undefined) clearTimeout(pending.readyTimer)
    if (pending.releaseTimer !== undefined) clearTimeout(pending.releaseTimer)
    if (pending.isolation !== undefined) {
      if (pending.isolation.ws !== undefined) clearTimeout(pending.isolation.ws.timer)
    }
    delete pending.isolation
    if (this.pending.get(pending.executionId) === pending) this.pending.delete(pending.executionId)
    if (pending.releaseFence) {
      this.released.add(pending.executionId)
      if (this.released.size > 1024) {
        const oldest = this.released.values().next().value
        if (typeof oldest === 'string') this.released.delete(oldest)
      }
    }
    this.sockets.delete(pending.socket)
    try { pending.socket.close() } catch { /* already closed */ }
  }

  /** Accepted transport failures must isolate the process before terminal. */
  private failAcceptedTransport(pending: Pending, errorCode: CodexSafeErrorCode): void {
    if (pending.preReadyIsolation) return
    if (pending.turnStartSent && pending.execution === undefined) {
      pending.preReadyIsolation = true
      void this.failDispatchedReservation(pending, `Codex bridge ${errorCode}`)
      return
    }
    if (pending.terminal) {
      if (pending.execution !== undefined) void this.isolateAfterReleaseFailure(pending)
      return
    }
    if (!pending.readySettled || pending.execution === undefined) {
      this.emitTerminal(pending, { type: 'terminal', status: 'failed', errorCode })
      return
    }
    void this.isolate(pending.execution).catch(() => {
      if (!pending.terminal) this.emitTerminal(pending, { type: 'terminal', status: 'failed', errorCode: 'isolation_failed' }, true)
    })
  }

  private async isolateViaHttp(execution: CodexBridgeExecution): Promise<CodexIsolationOutcome> {
    if (typeof fetch !== 'function') throw new Error('Codex host fetch is unavailable')
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), this.isolateTimeoutMs)
    try {
      const response = await fetch(`${this.bridgeUrl}/api/codex/turn/isolate`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          session_id: execution.sessionId,
          execution_id: execution.executionId,
          ...(execution.threadId.length === 0 ? {} : { thread_id: execution.threadId }),
          ...(execution.turnId.length === 0 ? {} : { turn_id: execution.turnId }),
        }),
        signal: controller.signal,
      })
      const contentLength = response.headers.get('content-length')
      if (contentLength !== null && Number(contentLength) > 16_384) throw new Error('Codex isolation response exceeded its limit')
      if (!response.ok) throw new Error('Codex process isolation was not acknowledged')
      const body = await response.text()
      if (body.length > 16_384) throw new Error('Codex isolation response exceeded its limit')
      let parsed: unknown
      try { parsed = JSON.parse(body) } catch { throw new Error('Codex process isolation was not acknowledged') }
      const value = record(parsed)
      if (value === undefined) {
        throw new Error('Codex process isolation was not acknowledged')
      }
      if (value['ok'] !== true || !exactHttpIdentity(execution, value)) {
        throw new Error('Codex process isolation identity was not acknowledged')
      }
      const outcome = isolationOutcome(value)
      if (outcome === undefined) throw new Error('Codex process isolation was not acknowledged')
      return outcome
    } finally {
      clearTimeout(timer)
    }
  }
}
