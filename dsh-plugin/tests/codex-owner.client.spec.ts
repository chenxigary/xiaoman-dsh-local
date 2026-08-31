import test from 'node:test'
import assert from 'node:assert/strict'
import { CodexOwnerQuarantine, type CodexOwner } from '../src/client/voice/codex-owner.ts'

const ownerA: CodexOwner = { sessionId: 'session-a', executionId: 'exec-a' }
const ownerB: CodexOwner = { sessionId: 'session-b', executionId: 'exec-b' }

test('Codex owner release is exact and idempotent while a late attempt is pending', async () => {
  const quarantine = new CodexOwnerQuarantine()
  let resolve!: () => void
  let calls = 0
  const pending = new Promise<void>(done => { resolve = done })
  const release = async (owner: CodexOwner): Promise<void> => {
    assert.deepEqual(owner, ownerA)
    calls += 1
    await pending
  }
  const first = quarantine.release(ownerA, release)
  const second = quarantine.release(ownerA, release)
  assert.strictEqual(first, second)
  await Promise.resolve()
  assert.equal(calls, 1)
  assert.deepEqual(quarantine.owners(), [ownerA])
  resolve()
  assert.equal(await first, true)
  assert.equal(quarantine.has(ownerA), false)
})

test('A to B and unmount retries retain a failed exact owner without clearing B', async () => {
  const quarantine = new CodexOwnerQuarantine()
  let fail = true
  const release = async (owner: CodexOwner): Promise<void> => {
    assert.equal(owner.sessionId, 'session-a')
    if (fail) throw new Error('maintenance still owns A')
  }
  assert.equal(await quarantine.release(ownerA, release), false)
  assert.equal(quarantine.isBlocked(ownerA), true)
  assert.equal(quarantine.quarantine(ownerB), true)
  assert.deepEqual(quarantine.owners(), [ownerA, ownerB])
  fail = false
  assert.equal(await quarantine.retryAll(release), 1)
  // B is intentionally not passed to the A-only release callback; exact
  // identity prevents a session reuse from releasing the wrong execution.
  assert.equal(quarantine.has(ownerA), false)
  assert.equal(quarantine.has(ownerB), true)
})

test('quarantine refuses only new owners at its hard bound and never evicts a blocked owner', () => {
  const quarantine = new CodexOwnerQuarantine()
  for (let index = 0; index < 32; index += 1) {
    assert.equal(quarantine.quarantine({ sessionId: `s-${index}`, executionId: `e-${index}` }), true)
  }
  assert.equal(quarantine.has({ sessionId: 's-0', executionId: 'e-0' }), true)
  assert.equal(quarantine.quarantine({ sessionId: 'overflow', executionId: 'overflow' }), false)
  assert.equal(quarantine.has({ sessionId: 's-31', executionId: 'e-31' }), true)
})
