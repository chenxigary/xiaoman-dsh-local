import test from 'node:test'
import assert from 'node:assert/strict'
import { CompanionStateMachine } from '../src/client/companion-state.ts'

test('companion lifecycle exposes the interrupt transition and reset', () => {
  const machine = new CompanionStateMachine()
  const seen: string[] = []
  machine.subscribe((state) => seen.push(state))
  machine.dispatch({ type: 'listen_start' })
  machine.dispatch({ type: 'thinking' })
  machine.dispatch({ type: 'speech_start' })
  machine.dispatch({ type: 'interrupted' })
  machine.dispatch({ type: 'reset' })
  assert.deepEqual(seen, ['LISTENING', 'THINKING', 'SPEAKING', 'INTERRUPTED', 'IDLE'])
})
