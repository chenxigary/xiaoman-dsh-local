import test from 'node:test'
import assert from 'node:assert/strict'
import {
  interruptAndAwaitCodexTerminal,
  interruptExactAndAwaitCodexTerminal,
  type CodexReleaseExecution,
  type CodexReleasePort,
} from '../src/client/voice/codex-release.ts'

function port(statuses: readonly (readonly CodexReleaseExecution[])[], interrupted: string[] = []): CodexReleasePort {
  let index = 0
  return {
    status: async () => ({ executions: statuses[Math.min(index++, statuses.length - 1)] ?? [] }),
    interrupt: async (_sessionId, executionId) => { interrupted.push(executionId) },
  }
}

test('exact Codex release interrupts a late-start owner before status visibility', async () => {
  const interrupted: string[] = []
  await interruptExactAndAwaitCodexTerminal(
    port([[]], interrupted),
    'session-a',
    'exec-late',
    { pollMs: 0, maxPolls: 1 },
  )
  assert.deepEqual(interrupted, ['exec-late'])
})

test('exact Codex release waits through running to terminal and preserves failures', async () => {
  const interrupted: string[] = []
  await interruptExactAndAwaitCodexTerminal(
    port([
      [{ executionId: 'exec-1', state: 'running' }],
      [{ executionId: 'exec-1', state: 'terminal' }],
      [],
    ], interrupted),
    'session-a',
    'exec-1',
    { pollMs: 0, maxPolls: 2 },
  )
  assert.deepEqual(interrupted, ['exec-1'])

  await interruptExactAndAwaitCodexTerminal(
    port([
      [{ executionId: 'exec-ack', state: 'terminal', released: true }],
    ], interrupted),
    'session-a',
    'exec-ack',
    { pollMs: 0, maxPolls: 1 },
  )

  const failing: CodexReleasePort = {
    status: async () => ({ executions: [{ executionId: 'exec-2', state: 'running' }] }),
    interrupt: async () => { throw new Error('release denied') },
  }
  await assert.rejects(
    () => interruptAndAwaitCodexTerminal(failing, 'session-a', { pollMs: 0, maxPolls: 1 }),
    /release denied/,
  )
})

test('terminal visibility alone does not release a maintenance owner', async () => {
  await assert.rejects(
    () => interruptExactAndAwaitCodexTerminal(
      port([[{ executionId: 'exec-blocked', state: 'terminal' }]], []),
      'session-a',
      'exec-blocked',
      { pollMs: 0, maxPolls: 1 },
    ),
    /仍未结束/,
  )
})

test('settling and blocked Host states remain owned until an explicit release ACK', async () => {
  for (const state of ['starting', 'settling', 'terminal', 'blocked'] as const) {
    await assert.rejects(
      () => interruptExactAndAwaitCodexTerminal(
        port([[{ executionId: `exec-${state}`, state }]], []),
        'session-a',
        `exec-${state}`,
        { pollMs: 0, maxPolls: 0 },
      ),
      /仍未结束/,
    )
  }
})
