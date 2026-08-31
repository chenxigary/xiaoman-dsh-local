import test from 'node:test'
import assert from 'node:assert/strict'
import {
  installComposerSubmitRoute,
  type RoutableComposerInput,
} from '../src/client/voice/native-composer-route.ts'

function input(): RoutableComposerInput & { nativeCalls: unknown[] } {
  const target: RoutableComposerInput & { nativeCalls: unknown[] } = {
    nativeCalls: [],
    state: { getSnapshot: () => ({ draft: 'hello', imageIds: [] }) },
    submit(mode) { target.nativeCalls.push(mode) },
    setDraft: () => {},
    notify: () => {},
  }
  return target
}

test('resident composer delegates unchanged while the alternate route declines', () => {
  const target = input()
  const dispose = installComposerSubmitRoute(target, () => false)
  target.submit('queue')
  assert.deepEqual(target.nativeCalls, ['queue'])
  dispose()
  target.submit('steer')
  assert.deepEqual(target.nativeCalls, ['queue', 'steer'])
})

test('resident composer is claimed without invoking the native sink', () => {
  const target = input()
  let claims = 0
  installComposerSubmitRoute(target, () => { claims += 1; return true })
  target.submit('queue')
  assert.equal(claims, 1)
  assert.deepEqual(target.nativeCalls, [])
})

test('disposal does not overwrite a newer router owner', () => {
  const target = input()
  let firstClaims = 0
  let secondChecks = 0
  const disposeFirst = installComposerSubmitRoute(target, () => { firstClaims += 1; return true })
  installComposerSubmitRoute(target, () => { secondChecks += 1; return false })
  disposeFirst()
  target.submit('queue')
  assert.equal(secondChecks, 1)
  assert.equal(firstClaims, 1)
  assert.deepEqual(target.nativeCalls, [])
})
