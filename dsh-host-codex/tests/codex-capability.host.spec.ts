import test from 'node:test'
import assert from 'node:assert/strict'
import {
  assertCodexTurnAvailable,
  codexTurnCapability,
  guardedCodexStart,
} from '../src/host/codex-capability.ts'

test('split-process credential isolation exposes read-only turns', () => {
  assert.equal(codexTurnCapability(), 'read-only')
  assert.doesNotThrow(() => assertCodexTurnAvailable('read-only'))
  assert.throws(
    () => assertCodexTurnAvailable('unavailable'),
    error => (error as { code?: string }).code === 'security_isolation_unavailable',
  )
})

test('security gate admits the audited read-only coordinator callback', async () => {
  let called = false
  const value = await guardedCodexStart('read-only', async () => {
      called = true
      return { executionId: 'exec-1' }
    })
  assert.deepEqual(value, { executionId: 'exec-1' })
  assert.equal(called, true)
})
