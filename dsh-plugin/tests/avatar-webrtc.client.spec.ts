import test from 'node:test'
import assert from 'node:assert/strict'
import {
  avatarRegistrationRequestInit,
  isAllowedAvatarBase,
} from '../src/client/voice/avatar-webrtc.ts'

test('Avatar signaling accepts only explicit loopback HTTP origins', () => {
  assert.equal(isAllowedAvatarBase('http://127.0.0.1:8010'), true)
  assert.equal(isAllowedAvatarBase('http://localhost:9000/'), true)
  assert.equal(isAllowedAvatarBase('http://[::1]:8010'), true)
  assert.equal(isAllowedAvatarBase('https://127.0.0.1:8010'), false)
  assert.equal(isAllowedAvatarBase('http://example.com:8010'), false)
  assert.equal(isAllowedAvatarBase('http://127.0.0.1:8010/path'), false)
})

test('Avatar unregister survives browser teardown without weakening registration', () => {
  const put = avatarRegistrationRequestInit('PUT', 'dsh-session', 'avatar-session')
  const remove = avatarRegistrationRequestInit('DELETE', 'dsh-session', 'avatar-session')

  assert.equal(put.keepalive, false)
  assert.equal(remove.keepalive, true)
  assert.equal(remove.body, JSON.stringify({
    dsh_session_id: 'dsh-session',
    avatar_session_id: 'avatar-session',
  }))
})
