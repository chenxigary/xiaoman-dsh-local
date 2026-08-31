import test from 'node:test'
import assert from 'node:assert/strict'
import {
  cancelCodexLoginBestEffort,
  getCodexLoginOwner,
  type CodexClient,
} from '../src/client/codex-remote-client.ts'

test('login cleanup keeps the exact captured owner and does not reuse an aborted poll signal', async () => {
  const calls: Array<{ sessionId: string; loginId: string; aborted: boolean }> = []
  const cancel: CodexClient['loginCancel'] = async (sessionId, loginId, signal) => {
    calls.push({ sessionId, loginId, aborted: signal?.aborted ?? false })
    return { loginId, status: 'canceled', success: false }
  }
  await cancelCodexLoginBestEffort(cancel, { sessionId: 'session-a', loginId: 'login-a' })
  assert.deepEqual(calls, [{ sessionId: 'session-a', loginId: 'login-a', aborted: false }])
  assert.equal(getCodexLoginOwner('session-a'), undefined)
})

test('timed-out login cancellation retains the exact owner for a remount retry', async () => {
  const owner = { sessionId: 'session-reconcile', loginId: 'login-reconcile' }
  let attempts = 0
  const first = await cancelCodexLoginBestEffort(
    async () => {
      attempts += 1
      return await new Promise<never>(() => {})
    },
    owner,
    1,
  )
  assert.equal(first, false)
  assert.deepEqual(getCodexLoginOwner(owner.sessionId), owner)

  const second = await cancelCodexLoginBestEffort(
    async (sessionId, loginId) => {
      attempts += 1
      return { sessionId, loginId, status: 'canceled', success: false }
    },
    owner,
    50,
  )
  assert.equal(second, true)
  assert.equal(attempts, 2)
  assert.equal(getCodexLoginOwner(owner.sessionId), undefined)
})

test('authoritative terminal cancellation outcome releases a stale owner idempotently', async () => {
  const owner = { sessionId: 'session-terminal', loginId: 'login-terminal' }
  const released = await cancelCodexLoginBestEffort(
    async () => ({ loginId: owner.loginId, status: 'not_found', success: false }),
    owner,
    50,
  )
  assert.equal(released, true)
  assert.equal(getCodexLoginOwner(owner.sessionId), undefined)
})
