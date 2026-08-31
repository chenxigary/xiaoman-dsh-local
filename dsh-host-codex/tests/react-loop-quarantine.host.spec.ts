import test from 'node:test'
import assert from 'node:assert/strict'
import { Context } from '../../.runtime/deepseek-harness/vendor/cordis/src/index.ts'
import LlmRuntime, { createUserMessage } from '../../.runtime/deepseek-harness/packages/llm/llm/src/index.ts'
import SessionStore, { SessionId } from '../../.runtime/deepseek-harness/packages/core/session/src/index.ts'
import SystemPrompt from '../../.runtime/deepseek-harness/packages/core/system-prompt/src/index.ts'
import ToolRuntime from '../../.runtime/deepseek-harness/packages/core/tools/src/index.ts'
import AgentRegistry from '../../.runtime/deepseek-harness/packages/core/agent/src/index.ts'
import AgentLoop from '../../.runtime/deepseek-harness/packages/core/agent-loop/src/index.ts'
import { MockAdapter, textResponse } from '../../.runtime/deepseek-harness/packages/core/agent-loop/tests/mock-adapter.ts'
import { CodexCoordinator } from '../src/host/codex-coordinator.ts'
import type { Agent } from '../../.runtime/deepseek-harness/packages/core/agent/src/index.ts'
import type {
  CodexBridgeEvent,
  CodexBridgeExecution,
  CodexBridgeTransport,
  CodexDurableSession,
} from '../src/types.ts'
import {
  CodexExecutionId,
  CodexSessionId,
  CodexThreadId,
  CodexTurnId,
} from '../src/types.ts'

class Bridge implements CodexBridgeTransport {
  readonly execution: CodexBridgeExecution = {
    executionId: CodexExecutionId('execution-real-agent'),
    sessionId: CodexSessionId('session-real-agent'),
    threadId: CodexThreadId('thread-real-agent'),
    turnId: CodexTurnId('turn-real-agent'),
  }
  onEvent: ((event: CodexBridgeEvent) => void) | undefined
  reserveCalls = 0
  interrupts: string[] = []
  isolates = 0
  isolateExecution: (sessionId: CodexSessionId, executionId: CodexExecutionId) => Promise<'released' | 'isolated'> = async () => 'isolated'
  async reserve(_request: Parameters<CodexBridgeTransport['reserve']>[0], onEvent: (event: CodexBridgeEvent) => void): Promise<CodexBridgeExecution> {
    this.reserveCalls += 1
    this.onEvent = onEvent
    onEvent({ type: 'started', threadId: this.execution.threadId, turnId: this.execution.turnId })
    return this.execution
  }
  async interrupt(_execution: CodexBridgeExecution, reason: string): Promise<void> {
    this.interrupts.push(reason)
    this.onEvent?.({ type: 'terminal', status: 'interrupted' })
  }
  async isolate(_execution: CodexBridgeExecution): Promise<'released' | 'isolated'> {
    this.isolates += 1
    return 'isolated'
  }
  finish(): void {
    this.onEvent?.({ type: 'terminal', status: 'completed', finalText: 'codex answer' })
  }
  async close(): Promise<void> {}
}

interface RealHarness {
  readonly ctx: Context
  readonly agent: Agent
  readonly adapter: MockAdapter
}

async function createRealHarness(id = 'session-real-agent'): Promise<RealHarness> {
  const ctx = new Context()
  await ctx.plugin(LlmRuntime)
  await ctx.plugin(SessionStore)
  await ctx.plugin(SystemPrompt)
  await ctx.plugin(ToolRuntime)
  await ctx.plugin(AgentRegistry)
  await ctx.plugin(AgentLoop, { agents: [] })
  const adapter = new MockAdapter([textResponse('native sentinel')])
  ctx.llm.registerAdapter(['mock'], adapter)
  const agent = ctx.agentLoop.create(SessionId(id), { provider: 'mock', model: 'mock' })
  return { ctx, agent, adapter }
}

async function eventually(predicate: () => boolean, timeoutMs = 2_000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (!predicate() && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 5))
  assert.equal(predicate(), true)
}

function queueWake(agent: Agent, text = 'native wake must stay parked'): void {
  agent.followup(createUserMessage({
    content: [{ type: 'text', text }],
    source: { kind: 'user' },
  }))
}

test('real ReactLoopAgent keeps terminal-flush quarantine through user cancel and releases on pre-whenIdle dispose', { timeout: 8_000 }, async () => {
  const { ctx, agent, adapter } = await createRealHarness()
  const bridge = new Bridge()
  let flushes = 0
  const coordinator = new CodexCoordinator(bridge, {
    id: () => 'execution-real-agent',
    flush: async () => { flushes += 1; return flushes < 2 },
  })
  try {
    const start = coordinator.start(agent, { text: 'codex turn', character: 'default' }, new AbortController().signal)
    await eventually(() => bridge.reserveCalls === 1)
    await start
    bridge.finish()
    await eventually(() => coordinator.status(agent.session.id)[0]?.state === 'blocked')

    queueWake(agent)
    await new Promise(resolve => setTimeout(resolve, 40))
    assert.equal(adapter.requests.length, 0)

    // ReactLoopAgent aborts maintenance for user cancellation too. That
    // abort must not release the coordinator's poisoned quarantine.
    agent.cancel({ kind: 'user' }, { keepInbox: true })
    await new Promise(resolve => setTimeout(resolve, 40))
    assert.equal(adapter.requests.length, 0)

    // The first cancellation cause wins for a ReactLoopAgent activity. Once
    // user cancellation has aborted the maintenance signal, the coordinator
    // observes the real pre-whenIdle `{ kind: 'disposed' }` cancel seam and
    // releases this poisoned hold without waking native work.
    await ctx.fiber.dispose()
    assert.equal(adapter.requests.length, 0)
  } finally {
    await coordinator.close()
    await ctx.fiber.dispose()
  }
})

test('real ReactLoopAgent disposal cause releases a quarantine when no earlier cancel won', { timeout: 8_000 }, async () => {
  const { ctx, agent, adapter } = await createRealHarness('session-dispose-real-agent')
  const bridge = new Bridge()
  let flushes = 0
  const coordinator = new CodexCoordinator(bridge, {
    id: () => 'execution-dispose-real-agent',
    flush: async () => { flushes += 1; return flushes < 2 },
  })
  try {
    const start = coordinator.start(agent, { text: 'codex turn', character: 'default' }, new AbortController().signal)
    await eventually(() => bridge.reserveCalls === 1)
    await start
    bridge.finish()
    await eventually(() => coordinator.status(agent.session.id)[0]?.state === 'blocked')
    queueWake(agent, 'dispose must not wake native turn')
    await new Promise(resolve => setTimeout(resolve, 40))
    assert.equal(adapter.requests.length, 0)

    // No prior abort has won the maintenance signal, so the real AgentLoop
    // disposal sends its structured `{ kind: 'disposed' }` cause and releases
    // the host quarantine without running the parked native wake.
    await ctx.fiber.dispose()
    assert.equal(adapter.requests.length, 0)
  } finally {
    await coordinator.close()
    await ctx.fiber.dispose()
  }
})

test('real ReactLoopAgent recovery hold ignores user cancel and coordinator.close releases it', { timeout: 8_000 }, async () => {
  const { ctx, agent, adapter } = await createRealHarness('session-recovery-real-agent')
  const bridge = new Bridge()
  bridge.isolateExecution = async () => { throw new Error('isolation unavailable') }
  let flushes = 0
  const coordinator = new CodexCoordinator(bridge, {
    flush: async () => { flushes += 1; return flushes < 2 },
  })
  coordinator.ensureAgentGuard(agent)
  const recoverySession: CodexDurableSession = {
    id: agent.session.id,
    events: [
      { type: 'codex/user-start', data: { executionId: 'old-execution', text: 'old', character: 'default' } },
      { type: 'codex/delegation-start', data: { executionId: 'old-execution', sessionId: 'session-recovery-real-agent', character: 'default' } },
    ],
    append() {},
  }
  try {
    const maintenance = agent.runMaintenance(async signal => {
      try {
        await coordinator.recover(recoverySession)
      } catch {
        await coordinator.holdRecovery(String(agent.session.id), signal, agent)
      }
    })
    await eventually(() => agent.inbox.nextTurn.length === 0)
    queueWake(agent, 'recovery wake must stay parked')
    await new Promise(resolve => setTimeout(resolve, 40))
    assert.equal(adapter.requests.length, 0)

    agent.cancel({ kind: 'user' }, { keepInbox: true })
    await new Promise(resolve => setTimeout(resolve, 40))
    assert.equal(adapter.requests.length, 0)

    await coordinator.close()
    await maintenance
    await agent.whenIdle()
    assert.equal(adapter.requests.length, 0)
  } finally {
    await coordinator.close()
    await ctx.fiber.dispose()
  }
})

test('Agent-owned poison survives coordinator recreation and blocks a late Host service', { timeout: 8_000 }, async () => {
  const { ctx, agent, adapter } = await createRealHarness('session-hmr-poison')
  const firstBridge = new Bridge()
  const first = new CodexCoordinator(firstBridge, {
    id: () => 'execution-hmr-poison',
    flush: async () => false,
  })
  try {
    await assert.rejects(
      first.start(agent, { text: 'poison me', character: 'default' }, new AbortController().signal),
      error => (error as { code?: string }).code === 'isolation_failed',
    )
    assert.equal(first.isAgentBlocked(agent), true)
    await first.close()

    const secondBridge = new Bridge()
    const second = new CodexCoordinator(secondBridge, { id: () => 'execution-hmr-new', flush: async () => true })
    try {
      await assert.rejects(
        second.start(agent, { text: 'must stay blocked', character: 'default' }, new AbortController().signal),
        error => (error as { code?: string }).code === 'isolation_failed',
      )
      assert.equal(secondBridge.reserveCalls, 0)
      queueWake(agent, 'late service native wake must be rejected')
      await new Promise(resolve => setTimeout(resolve, 40))
      assert.equal(adapter.requests.length, 0)
    } finally {
      await second.close()
    }
  } finally {
    await first.close()
    await ctx.fiber.dispose()
  }
})
