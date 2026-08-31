/** Client-only facade for the generated host-owned `codex` Remote namespace.
 *
 * This module deliberately contains no URL, WebSocket, fetch, or App Server
 * protocol code.  The only value crossing the client boundary is the
 * generated Typert Remote contribution mounted by `client/index.ts`.
 */
import type { SessionId } from '@deepseek-ai/dsh-client-runtime/client'
import type { RemoteResult, TypertClientRemote } from '@deepseek-ai/dsh-typert-protocol'
import type {
  CodexApprovalDecision,
  CodexApprovalResult,
  CodexCharacter,
  CodexLoginCancelResult,
  CodexLoginStartResult,
  CodexLoginStatusResult,
  CodexModelCatalogResult,
  CodexModelSelection,
  CodexRuntimeStatus,
  CodexStartResult,
} from '../types.ts'

// api/remotes is the sole Client assembly point for Host-generated namespaces.
// Keeping this import type-only preserves the browser boundary while making
// the compiler check every call against the generated host face.
import type {} from '@deepseek-ai/dsh-api-remotes/client'

const OFFICIAL_LOGIN_URL = 'https://chatgpt.com/auth/login' as const

export type CodexAuthState = 'ready' | 'signed_out' | 'unavailable'

export const MAX_CODEX_TEXT_CHARS = 8000

export interface CodexAuthStatus extends CodexRuntimeStatus {
  readonly state: CodexAuthState
}

export interface CodexClient {
  status(sessionId: string | undefined, signal?: AbortSignal): Promise<CodexAuthStatus>
  models(sessionId: string, signal?: AbortSignal): Promise<CodexModelCatalogResult>
  start(sessionId: string, request: {
    readonly text: string
    readonly character: CodexCharacter
  } & Partial<CodexModelSelection>, signal?: AbortSignal): Promise<CodexStartResult>
  interrupt(sessionId: string, executionId: string, signal?: AbortSignal): Promise<void>
  approvalDecision(
    sessionId: string,
    executionId: string,
    approvalId: string,
    decision: CodexApprovalDecision,
    signal?: AbortSignal,
  ): Promise<CodexApprovalResult>
  loginStart(sessionId: string, signal?: AbortSignal): Promise<CodexLoginStartResult>
  loginPending(sessionId: string, signal?: AbortSignal): Promise<CodexLoginStatusResult | null>
  loginStatus(sessionId: string, loginId: string, signal?: AbortSignal): Promise<CodexLoginStatusResult>
  loginCancel(sessionId: string, loginId: string, signal?: AbortSignal): Promise<CodexLoginCancelResult>
}

export interface CodexLoginOwner {
  readonly sessionId: string
  readonly loginId: string
}

// Login ownership is session-scoped rather than component-scoped.  A popup
// can outlive an input slot during HMR/session remount, and a timed-out cancel
// must remain recoverable by the next mounted owner.
const pendingLoginOwners = new Map<string, CodexLoginOwner>()
const loginCancelOperations = new Map<string, Promise<boolean>>()

export function rememberCodexLoginOwner(owner: CodexLoginOwner): void {
  pendingLoginOwners.set(owner.sessionId, owner)
}

export function getCodexLoginOwner(sessionId: string): CodexLoginOwner | undefined {
  return pendingLoginOwners.get(sessionId)
}

export function forgetCodexLoginOwner(owner: CodexLoginOwner): void {
  const current = pendingLoginOwners.get(owner.sessionId)
  if (current?.loginId === owner.loginId) pendingLoginOwners.delete(owner.sessionId)
}

/**
 * Cancel a login with a fresh signal.  A session switch/unmount aborts the
 * UI's polling signal, but that signal must never be reused for the exact
 * owner cleanup or the Host can retain a pending OAuth flow forever.
 */
export async function cancelCodexLoginBestEffort(
  cancel: CodexClient['loginCancel'],
  owner: CodexLoginOwner,
  timeoutMs = 2_000,
): Promise<boolean> {
  rememberCodexLoginOwner(owner)
  const existing = loginCancelOperations.get(owner.sessionId)
  if (existing !== undefined) return existing

  const operation = (async () => {
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    const cancelPromise = cancel(owner.sessionId, owner.loginId, controller.signal)
      .then(() => true, () => false)
    try {
      const canceled = await Promise.race([
        cancelPromise,
        new Promise<void>(resolve => {
          timer = globalThis.setTimeout(resolve, timeoutMs)
        }),
      ])
      if (canceled === true) forgetCodexLoginOwner(owner)
      return canceled === true
    } finally {
      if (timer !== undefined) globalThis.clearTimeout(timer)
      controller.abort()
    }
  })()
  loginCancelOperations.set(owner.sessionId, operation)
  try {
    return await operation
  } finally {
    if (loginCancelOperations.get(owner.sessionId) === operation) loginCancelOperations.delete(owner.sessionId)
  }
}

type CodexRemoteNamespace = TypertClientRemote['codex']

function safeError(code: string): Error & { readonly code: string } {
  const allowed = new Set([
    'not_authenticated', 'bridge_unavailable', 'bridge_protocol', 'turn_in_progress',
    'turn_failed', 'interrupt_timeout', 'invalid_request', 'approval_unavailable',
    'host_restart', 'interrupt_isolated', 'isolation_failed', 'mapping_commit_failed', 'security_isolation_unavailable',
    'internal_error', 'internal',
  ])
  const safeCode = allowed.has(code) ? code : 'internal_error'
  return Object.assign(new Error('Codex request could not be completed'), { code: safeCode }) as Error & { readonly code: string }
}

export function unavailableCodexStatus(): CodexAuthStatus {
  return {
    capability: 'unavailable',
    loggedIn: false,
    requiresOpenAiAuth: true,
    account: null,
    loginUrl: OFFICIAL_LOGIN_URL,
    state: 'unavailable',
    message: 'Codex bridge unavailable',
    executions: [],
  }
}

async function unwrap<T>(result: RemoteResult<T>): Promise<T> {
  if (!result.ok) throw safeError(result.error.code)
  return result.value
}

export function normalizeCodexAuthStatus(status: CodexRuntimeStatus): CodexAuthStatus {
  // Authentication identity and execution capability are orthogonal.  A
  // reachable execution-disabled bridge reports signed-out separately from a
  // transport failure.  Never turn a bridge outage into a misleading login
  // affordance.
  const authenticated = status.loggedIn && status.account !== null
  const bridgeUnavailable = status.message === 'Codex bridge unavailable'
  const state: CodexAuthState = bridgeUnavailable
    ? 'unavailable'
    : authenticated
      ? 'ready'
      : 'signed_out'
  return { ...status, state }
}

/** Strict navigation guard for the Host-provided official/OAuth/loopback URLs. */
export function isAllowedCodexAuthUrl(value: unknown): value is string {
  if (typeof value !== 'string' || value.length > 2048) return false
  try {
    const url = new URL(value)
    if (url.username !== '' || url.password !== '' || url.hash !== '') return false
    const host = url.hostname.toLowerCase()
    if (url.protocol === 'https:') {
      return (host === 'auth.openai.com' && url.pathname === '/oauth/authorize')
        || (host === 'chatgpt.com' && url.pathname === '/auth/login')
    }
    return url.protocol === 'http:'
      && host === 'localhost'
      && url.port === '1455'
      && url.pathname === '/auth/callback'
  } catch {
    return false
  }
}

/** Stable UI fallback/copy; actual login navigation uses typed loginStart. */
export const codexLoginUrl = OFFICIAL_LOGIN_URL

/** Bind the generated namespace to the small face consumed by UI controls. */
export function createCodexClient(remote: TypertClientRemote): CodexClient {
  const namespace = remote.codex as CodexRemoteNamespace
  return {
    async status(sessionId, signal) {
      if (sessionId === undefined) {
        return unavailableCodexStatus()
      }
      return normalizeCodexAuthStatus(await unwrap(await namespace.status(sessionId as SessionId, signal)))
    },
    async models(sessionId, signal) {
      return await unwrap(await namespace.models(sessionId as SessionId, signal))
    },
    async start(sessionId, request, signal) {
      if (request.text.trim() === '' || request.text.length > MAX_CODEX_TEXT_CHARS) {
        throw safeError('invalid_request')
      }
      return await unwrap(await namespace.start(sessionId as SessionId, request, signal))
    },
    async interrupt(sessionId, executionId, signal) {
      await unwrap(await namespace.interrupt(sessionId as SessionId, executionId, signal))
    },
    async approvalDecision(sessionId, executionId, approvalId, decision, signal) {
      return await unwrap(await namespace.approvalDecision(
        sessionId as SessionId, executionId, approvalId, decision, signal,
      ))
    },
    async loginStart(sessionId, signal) {
      return await unwrap(await namespace.loginStart(sessionId as SessionId, signal))
    },
    async loginPending(sessionId, signal) {
      return await unwrap(await namespace.loginPending(sessionId as SessionId, signal))
    },
    async loginStatus(sessionId, loginId, signal) {
      return await unwrap(await namespace.loginStatus(sessionId as SessionId, loginId, signal))
    },
    async loginCancel(sessionId, loginId, signal) {
      return await unwrap(await namespace.loginCancel(sessionId as SessionId, loginId, signal))
    },
  }
}
