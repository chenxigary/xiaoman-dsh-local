import test from 'node:test'
import assert from 'node:assert/strict'
import { shouldInterruptCodex } from '../src/client/voice/codex-gate.ts'

test('DSH mic start skips Codex status/auth when the bridge is unavailable', () => {
  assert.equal(shouldInterruptCodex('dsh', false), false)
  assert.equal(shouldInterruptCodex('dsh', true), false)
})

test('Codex mic release is eligible only for an explicitly known owner', () => {
  assert.equal(shouldInterruptCodex('codex', false), false)
  assert.equal(shouldInterruptCodex('codex', true), true)
})
