import test from 'node:test'
import assert from 'node:assert/strict'
import { canSelectCodex, canShowCodexLogin } from '../src/client/codex-auth-gate.ts'
import { isAllowedCodexAuthUrl, normalizeCodexAuthStatus } from '../src/client/codex-remote-client.ts'

test('Codex auth gate distinguishes ready, signed-out, and unavailable', () => {
  assert.equal(canSelectCodex({ state: 'ready' }), true)
  assert.equal(canShowCodexLogin({ state: 'ready' }), false)
  assert.equal(canSelectCodex({ state: 'signed_out' }), true)
  assert.equal(canShowCodexLogin({ state: 'signed_out' }), true)
  assert.equal(canSelectCodex({ state: 'unavailable' }), false)
  assert.equal(canShowCodexLogin({ state: 'unavailable' }), false)
  assert.equal(canSelectCodex(null), false)
  assert.equal(canShowCodexLogin(null), false)
})

test('logged-out execution-unavailable status still exposes login but never enables Codex', () => {
  const status = { state: 'signed_out' as const, capability: 'unavailable' as const }
  assert.equal(canShowCodexLogin(status), true)
  assert.equal(canSelectCodex(status), false)
})

test('login navigation accepts only known host/path pairs', () => {
  assert.equal(isAllowedCodexAuthUrl('https://chatgpt.com/auth/login'), true)
  assert.equal(isAllowedCodexAuthUrl('https://chatgpt.com/other'), false)
  assert.equal(isAllowedCodexAuthUrl('https://evil.chatgpt.com/auth/login'), false)
  assert.equal(isAllowedCodexAuthUrl('https://auth.openai.com/oauth/authorize?client_id=codex&state=s'), true)
  assert.equal(isAllowedCodexAuthUrl('https://auth.openai.com/other'), false)
  assert.equal(isAllowedCodexAuthUrl('https://login.openai.com/oauth/authorize'), false)
  assert.equal(isAllowedCodexAuthUrl('http://localhost:1455/auth/callback?code=c&state=s'), true)
  assert.equal(isAllowedCodexAuthUrl('http://127.0.0.1:1455/auth/callback?code=c&state=s'), false)
  assert.equal(isAllowedCodexAuthUrl('http://127.0.0.1:8765/auth/login'), false)
  assert.equal(isAllowedCodexAuthUrl('http://localhost:1455/other'), false)
})

function runtimeStatus(overrides: Record<string, unknown>): Parameters<typeof normalizeCodexAuthStatus>[0] {
  return {
    capability: 'unavailable',
    loggedIn: false,
    requiresOpenAiAuth: true,
    account: null,
    loginUrl: 'https://chatgpt.com/auth/login',
    executions: [],
    ...overrides,
  } as Parameters<typeof normalizeCodexAuthStatus>[0]
}

test('bridge-unavailable status stays unavailable while reachable execution-disabled signed-out allows login', () => {
  assert.equal(normalizeCodexAuthStatus(runtimeStatus({ message: 'Codex bridge unavailable' })).state, 'unavailable')
  assert.equal(normalizeCodexAuthStatus(runtimeStatus({ message: 'ChatGPT login required' })).state, 'signed_out')
  assert.equal(normalizeCodexAuthStatus(runtimeStatus({ message: 'Codex turn execution unavailable' })).state, 'signed_out')
  const authenticatedUnavailable = normalizeCodexAuthStatus(runtimeStatus({ loggedIn: true, account: { type: 'chatgpt', planType: 'plus' }, message: 'Codex turn execution unavailable' }))
  assert.equal(authenticatedUnavailable.state, 'ready')
  assert.equal(canSelectCodex(authenticatedUnavailable), false)
})
