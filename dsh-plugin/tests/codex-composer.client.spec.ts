import test from 'node:test'
import assert from 'node:assert/strict'
import { createCodexStartGate } from '../src/client/codex-start-gate.ts'

test('Codex start gate admits one of two immediate submit attempts', () => {
  const gate = createCodexStartGate()
  let rpcCalls = 0
  const submit = (): boolean => {
    if (!gate.tryClaim()) return false
    rpcCalls += 1
    return true
  }

  assert.equal(submit(), true)
  assert.equal(submit(), false)
  assert.equal(rpcCalls, 1)
  assert.equal(gate.claimed, true)
  gate.release()
  assert.equal(submit(), true)
  assert.equal(rpcCalls, 2)
})
