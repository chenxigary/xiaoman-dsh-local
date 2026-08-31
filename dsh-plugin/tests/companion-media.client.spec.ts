import test from 'node:test'
import assert from 'node:assert/strict'
import { avatarPlaybackIsStalled, shouldLoopIdleVideo } from '../src/client/voice/companion-media.ts'

test('one idle clip loops while a playlist advances through ended events', () => {
  assert.equal(shouldLoopIdleVideo(0), false)
  assert.equal(shouldLoopIdleVideo(1), true)
  assert.equal(shouldLoopIdleVideo(2), false)
})

test('avatar playback watchdog rejects stopped or unready WebRTC video', () => {
  assert.equal(avatarPlaybackIsStalled(10, 14, false, 4), false)
  assert.equal(avatarPlaybackIsStalled(10, 10.1, false, 4), true)
  assert.equal(avatarPlaybackIsStalled(10, 14, true, 4), true)
  assert.equal(avatarPlaybackIsStalled(10, 14, false, 1), true)
  assert.equal(avatarPlaybackIsStalled(Number.NaN, 14, false, 4), true)
})
