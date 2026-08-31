import test from 'node:test'
import assert from 'node:assert/strict'
import { UtteranceGeneration } from '../src/client/voice/turn-generation.ts'

test('deferred STT from an old mode cannot start Codex, while the next utterance uses the new owner refs', async () => {
  const fence = new UtteranceGeneration()
  const firstGeneration = fence.current
  const firstStt = new AbortController()
  fence.track(firstStt)
  let resolveFirst: (() => void) | undefined
  const firstResult = new Promise<void>(resolve => { resolveFirst = resolve })
  let mode: string = 'codex'
  let character: string = 'default'
  let start: { mode: string; character: string } | undefined

  // The synchronous owner cancellation happens before the mode commit.
  fence.cancel()
  mode = 'dsh'
  character = 'xiaoman'
  resolveFirst?.()
  await firstResult
  if (fence.isCurrent(firstGeneration) && !firstStt.signal.aborted && mode === 'codex') {
    start = { mode, character }
  }
  assert.equal(firstStt.signal.aborted, true)
  assert.equal(start, undefined)

  // A later utterance gets a fresh generation and reads current mode/character.
  const secondGeneration = fence.current
  const secondStt = new AbortController()
  fence.track(secondStt)
  mode = 'codex'
  character = 'default'
  if (fence.isCurrent(secondGeneration) && !secondStt.signal.aborted && mode === 'codex') {
    start = { mode, character }
  }
  assert.deepEqual(start, { mode: 'codex', character: 'default' })
})
