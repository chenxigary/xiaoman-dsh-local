import test from 'node:test'
import assert from 'node:assert/strict'
import { CodexCoordinator } from '../src/host/codex-coordinator.ts'
import type {
  CodexBridgeEvent,
  CodexBridgeExecution,
  CodexBridgeTransport,
  CodexDurableSession,
  CodexMaintenanceAgent,
} from '../src/types.ts'
import {
  CodexExecutionId,
  CodexSessionId,
  CodexThreadId,
  CodexTurnId,
} from '../src/types.ts'

class Session implements CodexDurableSession {
  readonly events: Array<{ type: string; data: unknown }> = []
  readonly id: string
  constructor(id = 'session-1') { this.id = id }
  append(type: string, data: unknown): void { this.events.push({ type, data }) }
}

class Agent implements CodexMaintenanceAgent {
  readonly session: Session
  readonly cancels: unknown[] = []
  maintenance = 0
  constructor(session: Session = new Session()) { this.session = session }
  runMaintenance<T>(task: (signal: AbortSignal) => Promise<T>): Promise<T> {
    this.maintenance += 1
    const signal = new AbortController().signal
    return task(signal).finally(() => { this.maintenance -= 1 })
  }
  cancel(cause: unknown, options?: { keepInbox?: boolean }): void {
    this.cancels.push({ cause, options })
  }
}

class BusyGuardAgent implements CodexMaintenanceAgent {
  readonly session: Session
  readonly preStepListeners: Array<(payload: { agent?: unknown }, next: () => Promise<unknown>) => Promise<unknown>> = []
  readonly ctx: NonNullable<CodexMaintenanceAgent['ctx']> = {
    on: (_name: string, listener: (payload: { agent?: unknown }, next: () => Promise<unknown>) => Promise<unknown>) => {
      this.preStepListeners.push(listener)
      return () => undefined
    },
  } as unknown as NonNullable<CodexMaintenanceAgent['ctx']>
  constructor(session: Session) { this.session = session }
  runMaintenance<T>(_task: (signal: AbortSignal) => Promise<T>): Promise<T> {
    return Promise.reject(new Error('agent busy'))
  }
  cancel(): void {}
}

class Bridge implements CodexBridgeTransport {
  readonly execution: CodexBridgeExecution = {
    executionId: CodexExecutionId('execution-1'),
    sessionId: CodexSessionId('session-1'),
    threadId: CodexThreadId('thread-1'),
    turnId: CodexTurnId('turn-1'),
  }
  onEvent: ((event: CodexBridgeEvent) => void) | undefined
  reserveGate: Promise<void> = Promise.resolve()
  reserveCalls = 0
  interrupts: string[] = []
  isolates = 0
  isolateExecution: (sessionId: CodexSessionId, executionId: CodexExecutionId) => Promise<'released' | 'isolated'> = async () => 'isolated'
  async reserve(_request: Parameters<CodexBridgeTransport['reserve']>[0], onEvent: (event: CodexBridgeEvent) => void): Promise<CodexBridgeExecution> {
    this.reserveCalls += 1
    this.onEvent = onEvent
    await this.reserveGate
    onEvent({ type: 'started', threadId: this.execution.threadId, turnId: this.execution.turnId })
    return this.execution
  }
  async interrupt(_execution: CodexBridgeExecution, reason: string): Promise<void> {
    this.interrupts.push(reason)
    this.onEvent?.({ type: 'terminal', status: 'interrupted' })
  }
  async isolate(_execution: CodexBridgeExecution): Promise<'released' | 'isolated'> { this.isolates += 1; return 'isolated' }
  finish(text = 'done'): void { this.onEvent?.({ type: 'terminal', status: 'completed', finalText: text }) }
  delta(text: string): void { this.onEvent?.({ type: 'text_delta', phase: 'final_answer', text, speakable: true }) }
}

async function eventually(predicate: () => boolean, timeoutMs = 1_000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (!predicate() && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 1))
  assert.equal(predicate(), true)
}

test('same session rejects concurrent start and keeps ownership until maintenance settles', async () => {
  const agent = new Agent()
  const bridge = new Bridge()
  const coordinator = new CodexCoordinator(bridge, { id: () => 'execution-1', flush: async () => true })
  const first = coordinator.start(agent, { text: 'first', character: 'default' }, new AbortController().signal)
  await eventually(() => bridge.reserveCalls === 1)
  await assert.rejects(
    coordinator.start(agent, { text: 'second', character: 'default' }, new AbortController().signal),
    error => (error as { code?: string }).code === 'turn_in_progress',
  )
  const accepted = await first
  assert.equal(accepted.executionId, 'execution-1')
  bridge.finish('answer')
  await eventually(() => coordinator.status('session-1').length === 0)
  const next = coordinator.start(agent, { text: 'second', character: 'default' }, new AbortController().signal)
  assert.equal((await next).executionId, 'execution-1')
  bridge.finish('second answer')
  await eventually(() => coordinator.status('session-1').length === 0)
})

test('early abort remains armed until reserve identity exists and sends exact interrupt', async () => {
  const agent = new Agent()
  const bridge = new Bridge()
  let release: () => void = () => {}
  bridge.reserveGate = new Promise(resolve => { release = resolve })
  const coordinator = new CodexCoordinator(bridge, { id: () => 'execution-1', flush: async () => true })
  const controller = new AbortController()
  const start = coordinator.start(agent, { text: 'cancel me', character: 'default' }, controller.signal)
  await eventually(() => bridge.reserveCalls === 1)
  controller.abort()
  release()
  await start
  await eventually(() => bridge.interrupts.length === 1)
  assert.deepEqual(agent.cancels, [{ cause: { kind: 'hook', reason: 'codex:barge-in' }, options: { keepInbox: true } }])
  await eventually(() => coordinator.status('session-1').length === 0)
})

test('mid-stream durable failure isolates and poisons the session without a fake terminal', async () => {
  const agent = new Agent()
  const bridge = new Bridge()
  let flushes = 0
  const coordinator = new CodexCoordinator(bridge, {
    id: () => 'execution-1',
    flush: async () => { flushes += 1; return flushes < 2 },
  })
  const start = coordinator.start(agent, { text: 'stream', character: 'default' }, new AbortController().signal)
  await eventually(() => bridge.reserveCalls === 1)
  await start
  bridge.delta('one')
  await eventually(() => bridge.isolates >= 1)
  assert.equal(coordinator.status('session-1')[0]?.state, 'blocked')
  // The poisoned Agent returns to idle; its agent-owned pre-step guard is
  // the persistent quarantine and prevents native LLM/tool work.
  assert.equal(agent.maintenance, 0)
  assert.equal(agent.session.events.filter(event => event.type === 'codex/terminal').length, 0)
  await assert.rejects(
    coordinator.start(agent, { text: 'must reconcile', character: 'default' }, new AbortController().signal),
    error => (error as { code?: string }).code === 'isolation_failed',
  )
  await coordinator.close()
})

test('start flush false poisons before reservation and rejects future turns', async () => {
  const agent = new Agent()
  const bridge = new Bridge()
  const coordinator = new CodexCoordinator(bridge, {
    id: () => 'execution-1',
    flush: async () => false,
  })
  await assert.rejects(
    coordinator.start(agent, { text: 'not durable', character: 'default' }, new AbortController().signal),
    error => (error as { code?: string }).code === 'isolation_failed',
  )
  assert.equal(bridge.reserveCalls, 0)
  assert.equal(coordinator.status('session-1')[0]?.state, 'blocked')
  assert.equal(agent.maintenance, 0)
  await assert.rejects(
    coordinator.start(agent, { text: 'blocked', character: 'default' }, new AbortController().signal),
    error => (error as { code?: string }).code === 'isolation_failed',
  )
  await coordinator.close()
})

test('terminal flush false keeps the execution blocked after process isolation', async () => {
  const agent = new Agent()
  const bridge = new Bridge()
  let flushes = 0
  const coordinator = new CodexCoordinator(bridge, {
    id: () => 'execution-1',
    flush: async () => { flushes += 1; return flushes < 2 },
  })
  const start = coordinator.start(agent, { text: 'terminal boundary', character: 'default' }, new AbortController().signal)
  await eventually(() => bridge.reserveCalls === 1)
  await start
  bridge.finish('answer')
  await eventually(() => bridge.isolates >= 1)
  assert.equal(coordinator.status('session-1')[0]?.state, 'blocked')
  assert.equal(agent.maintenance, 0)
  assert.equal(agent.session.events.filter(event => event.type === 'codex/terminal').length, 1)
  await assert.rejects(
    coordinator.start(agent, { text: 'reconcile first', character: 'default' }, new AbortController().signal),
    error => (error as { code?: string }).code === 'isolation_failed',
  )
  await coordinator.close()
})

test('interrupt-intent flush false never dispatches a bridge interrupt', async () => {
  const agent = new Agent()
  const bridge = new Bridge()
  let flushes = 0
  const coordinator = new CodexCoordinator(bridge, {
    id: () => 'execution-1',
    flush: async () => { flushes += 1; return flushes < 2 },
  })
  const controller = new AbortController()
  const start = coordinator.start(agent, { text: 'intent boundary', character: 'default' }, controller.signal)
  await eventually(() => bridge.reserveCalls === 1)
  await start
  controller.abort()
  await eventually(() => bridge.isolates >= 1)
  assert.deepEqual(bridge.interrupts, [])
  assert.equal(coordinator.status('session-1')[0]?.state, 'blocked')
  assert.equal(agent.maintenance, 0)
  await coordinator.close()
})

test('restart isolation failure quarantines maintenance instead of waking native loop', async () => {
  const agent = new Agent()
  agent.session.events.push(
    { type: 'codex/user-start', data: { executionId: 'old-execution', text: 'old', character: 'default' } },
    { type: 'codex/delegation-start', data: { executionId: 'old-execution', sessionId: 'session-1', character: 'default' } },
  )
  const bridge = new Bridge()
  bridge.isolateExecution = async () => { throw new Error('old process unavailable') }
  const coordinator = new CodexCoordinator(bridge, { id: () => 'execution-1', flush: async () => true })
  await assert.rejects(
    coordinator.start(agent, { text: 'reconcile', character: 'default' }, new AbortController().signal),
    error => (error as { code?: string }).code === 'isolation_failed',
  )
  assert.equal(agent.maintenance, 0)
  assert.equal(bridge.reserveCalls, 0)
  await coordinator.close()
  assert.equal(agent.maintenance, 0)
})

test('restart released outcome without a durable terminal stays quarantined', async () => {
  const agent = new Agent()
  agent.session.events.push(
    { type: 'codex/user-start', data: { executionId: 'old-execution', text: 'old', character: 'default' } },
    { type: 'codex/delegation-start', data: { executionId: 'old-execution', sessionId: 'session-1', character: 'default' } },
  )
  const bridge = new Bridge()
  bridge.isolateExecution = async () => 'released'
  const coordinator = new CodexCoordinator(bridge, { id: () => 'execution-1', flush: async () => true })
  await assert.rejects(
    coordinator.start(agent, { text: 'reconcile', character: 'default' }, new AbortController().signal),
    error => (error as { code?: string }).code === 'isolation_failed',
  )
  assert.equal(bridge.reserveCalls, 0)
  assert.equal(agent.session.events.some(event => event.type === 'codex/terminal'), false)
  await coordinator.close()
})

test('startup preflight poisons before a busy maintenance claim and rejects native pre-step', async () => {
  const session = new Session('session-busy-recovery')
  session.events.push(
    { type: 'codex/user-start', data: { executionId: 'old-execution', text: 'old', character: 'default' } },
    { type: 'codex/delegation-start', data: { executionId: 'old-execution', sessionId: session.id, character: 'default' } },
  )
  const agent = new BusyGuardAgent(session)
  const bridge = new Bridge()
  const coordinator = new CodexCoordinator(bridge, { flush: async () => true })

  assert.equal(coordinator.prepareAgentRecovery(agent), true)
  assert.equal(coordinator.isBlocked(session.id), true)
  assert.equal(agent.preStepListeners.length, 1)
  let nativeSteps = 0
  const result = await agent.preStepListeners[0]!({ agent }, async () => {
    nativeSteps += 1
    return { kind: 'next' }
  })
  assert.deepEqual(result, { kind: 'reject' })
  assert.equal(nativeSteps, 0)
  await assert.rejects(
    coordinator.start(agent, { text: 'must remain quarantined', character: 'default' }, new AbortController().signal),
    error => (error as { code?: string }).code === 'isolation_failed',
  )
  coordinator.reconcileAgentRecovery(agent)
  const released = await agent.preStepListeners[0]!({ agent }, async () => {
    nativeSteps += 1
    return { kind: 'next' }
  })
  assert.deepEqual(released, { kind: 'next' })
  assert.equal(nativeSteps, 1)
  await coordinator.close()
})
