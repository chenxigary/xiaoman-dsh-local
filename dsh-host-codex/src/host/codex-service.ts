/** Typert Host Remote facade for Codex delegated turns. */

import type { Context } from '@deepseek-ai/cordis'
import type { Agent } from '@deepseek-ai/dsh-agent'
import z from '@deepseek-ai/schemastery'
import { TypertRemoteService, Remote } from '@deepseek-ai/dsh-typert-protocol'
import { parseCodexLoginId } from '../types.ts'
import type {
  CodexAccountProof,
  CodexApprovalDecision,
  CodexApprovalResult,
  CodexRuntimeStatus,
  CodexStartRequest,
  CodexStartResult,
  CodexStatus,
  CodexInterruptResult,
  CodexLoginCancelResult,
  CodexLoginStartResult,
  CodexLoginStatusResult,
  CodexModelCatalogResult,
  CodexModelOption,
} from '../types.ts'
import { CodexCoordinator } from './codex-coordinator.ts'
import { WebSocketCodexBridgeTransport } from './codex-bridge.ts'
import { codexTurnCapability, guardedCodexStart } from './codex-capability.ts'

const LOGIN_URL = 'https://chatgpt.com/auth/login' as const
const MAX_CONTROL_RESPONSE_BYTES = 32 * 1024
const LOGIN_START_MAX_BODY_BYTES = 256
const UUID_V4_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const LOGIN_OWNER_REGISTRY_KEY = Symbol.for('deepseek-harness.codex.login-owner-registry')

interface LoginOwner {
  readonly sessionId: string
  /** Present for the service that started the flow; absent after HMR reload. */
  readonly agent?: Agent
}

// Cordis service disposal does not imply that the bridge's OAuth child has
// stopped. Keep ownership on the process global so a service instance created
// by HMR can still poll/cancel the exact pending flow rather than opening a
// second operation or silently forgetting the backend owner.
const LOGIN_OWNER_REGISTRY: Map<string, LoginOwner> = (() => {
  const globalState = globalThis as unknown as Record<PropertyKey, unknown>
  const existing = globalState[LOGIN_OWNER_REGISTRY_KEY]
  if (existing instanceof Map) return existing as Map<string, LoginOwner>
  const created = new Map<string, LoginOwner>()
  globalState[LOGIN_OWNER_REGISTRY_KEY] = created
  return created
})()

function safeBridgeBase(value: string): string {
  let url: URL
  try { url = new URL(value) } catch { throw new Error('Codex bridge URL is invalid') }
  const host = url.hostname.toLowerCase()
  const loopback = host === 'localhost' || host === '127.0.0.1' || host === '[::1]' || host === '::1'
  if (!loopback || (url.protocol !== 'http:' && url.protocol !== 'https:')
    || url.username !== '' || url.password !== '' || url.search !== '' || url.hash !== ''
    || (url.pathname !== '' && url.pathname !== '/')) {
    throw new Error('Codex bridge URL must be a loopback HTTP(S) origin')
  }
  return url.toString().replace(/\/$/, '')
}

export interface Config {
  /** Host-only bridge base; never supplied by the browser. */
  readonly bridgeUrl: string
  /** Bound before a turn can be accepted. */
  readonly readyTimeoutMs: number
  /** Bound for interrupt acknowledgement before process isolation. */
  readonly interruptTimeoutMs: number
  /** Bound for the process-facing isolate ACK. */
  readonly isolateTimeoutMs: number
  /** Bound for provider cleanup after a normal terminal. */
  readonly releaseTimeoutMs: number
}

function safeLoginId(value: unknown) {
  const loginId = parseCodexLoginId(value)
  // The backend login route permits the full 256-byte business bound. Keep
  // the Host parser at the same bound so a valid long-lived OAuth owner cannot
  // be turned into an unknown-owner quarantine by a narrower UI limit.
  return loginId !== undefined && /^[A-Za-z0-9_-]{1,256}$/.test(loginId) ? loginId : undefined
}

function safeOperationId(value: unknown): string | undefined {
  return typeof value === 'string' && UUID_V4_RE.test(value) ? value : undefined
}

function createOperationId(): string {
  const operationId = globalThis.crypto?.randomUUID?.()
  if (operationId === undefined || safeOperationId(operationId) === undefined) {
    throw Object.assign(new Error('Codex login operation is unavailable'), { code: 'bridge_unavailable' })
  }
  return operationId
}

function safeAuthUrl(value: unknown): string | undefined {
  if (typeof value !== 'string' || value.length > 2048) return undefined
  try {
    const url = new URL(value)
    if (url.username !== '' || url.password !== '' || url.hash !== '') return undefined
    const host = url.hostname.toLowerCase()
    if (url.protocol === 'https:') {
      // Pinned App Server 0.148 returns this exact official OAuth route;
      // accepting arbitrary OpenAI/ChatGPT paths would turn a backend drift
      // or compromised response into an untrusted popup navigation.
      if (host !== 'auth.openai.com' || url.pathname !== '/oauth/authorize') return undefined
      return url.toString()
    }
    if (url.protocol === 'http:' && (host === 'localhost' || host === '127.0.0.1' || host === '[::1]' || host === '::1')
      && url.pathname === '/oauth/callback') return url.toString()
  } catch { /* safe boundary */ }
  return undefined
}

function objectOf(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : undefined
}

function strictChatgptAccount(value: unknown): CodexAccountProof | null {
  const account = objectOf(value)
  const planTypes = new Set([
    'free', 'go', 'plus', 'pro', 'prolite', 'team',
    'self_serve_business_prolite', 'self_serve_business_usage_based',
    'business', 'ent26', 'enterprise_cbp_automation',
    'enterprise_cbp_usage_based', 'enterprise', 'edu', 'unknown',
  ])
  if (account === undefined || account['type'] !== 'chatgpt' || typeof account['planType'] !== 'string' || !planTypes.has(account['planType'])) return null
  return { type: 'chatgpt', planType: account['planType'] as CodexAccountProof['planType'] }
}

function boundedText(value: unknown, maximum: number): string | undefined {
  return typeof value === 'string' && value.length > 0 && value.length <= maximum ? value : undefined
}

/** Revalidate the bridge projection before it crosses the generated Remote. */
function strictModelCatalog(value: unknown): CodexModelCatalogResult | undefined {
  const root = objectOf(value)
  const rawModels = root?.['models']
  if (!Array.isArray(rawModels) || rawModels.length === 0 || rawModels.length > 32) return undefined
  const models: CodexModelOption[] = []
  for (const raw of rawModels) {
    const model = objectOf(raw)
    const id = boundedText(model?.['id'], 128)
    const displayName = boundedText(model?.['displayName'], 128)
    const description = boundedText(model?.['description'], 512)
    const defaultReasoningEffort = boundedText(model?.['defaultReasoningEffort'], 32)
    const rawEfforts = model?.['supportedReasoningEfforts']
    const rawTiers = model?.['serviceTiers']
    if (id === undefined || displayName === undefined || description === undefined || defaultReasoningEffort === undefined
      || !Array.isArray(rawEfforts) || rawEfforts.length === 0 || rawEfforts.length > 16
      || !Array.isArray(rawTiers) || rawTiers.length > 8) return undefined
    const efforts = rawEfforts.map((rawEffort) => {
      const effort = objectOf(rawEffort)
      const effortId = boundedText(effort?.['id'], 32)
      const effortDescription = boundedText(effort?.['description'], 256)
      return effortId === undefined || effortDescription === undefined ? undefined : { id: effortId, description: effortDescription }
    })
    const tiers = rawTiers.map((rawTier) => {
      const tier = objectOf(rawTier)
      const tierId = boundedText(tier?.['id'], 64)
      const name = boundedText(tier?.['name'], 64)
      const tierDescription = boundedText(tier?.['description'], 256)
      return tierId === undefined || name === undefined || tierDescription === undefined
        ? undefined
        : { id: tierId, name, description: tierDescription }
    })
    if (efforts.some(item => item === undefined) || tiers.some(item => item === undefined)
      || !efforts.some(item => item?.id === defaultReasoningEffort)) return undefined
    models.push({
      id,
      displayName,
      description,
      defaultReasoningEffort,
      supportedReasoningEfforts: efforts as { id: string; description: string }[],
      serviceTiers: tiers as { id: string; name: string; description: string }[],
    })
  }
  return { models }
}

/**
 * Host service.  The bridge remains behind this typed namespace; browser code
 * has no URL, WebSocket, App Server, or token access.
 */
export class CodexService extends TypertRemoteService {
  static inject = ['agents', 'sessions']
  static Config: z<Config> = z.object({
    bridgeUrl: z.string().default('http://127.0.0.1:8765'),
    readyTimeoutMs: z.natural().min(100).max(30_000).default(10_000),
    interruptTimeoutMs: z.natural().min(100).max(120_000).default(2_000),
    isolateTimeoutMs: z.natural().min(100).max(30_000).default(5_000),
    releaseTimeoutMs: z.natural().min(100).max(120_000).default(10_000),
  })

  private readonly coordinator: CodexCoordinator
  private readonly bridgeUrl: string
  private readonly controlTimeoutMs: number
  private readonly loginOwners = LOGIN_OWNER_REGISTRY
  private loginStartInFlight = false
  /** A timed-out start has an unknown backend owner and remains fail-closed. */
  private loginStartUncertain = false
  private readonly recoveryAgents = new WeakSet<object>()
  private readonly recoveryAttempts = new WeakMap<object, number>()
  private readonly recoveryTimers = new Map<object, ReturnType<typeof setTimeout>>()
  private closePromise: Promise<void> | undefined
  /** Resolves after an accepted-but-not-yet-replied login POST is reconciled. */
  private loginStartSettled: Promise<void> | undefined
  private closing = false

  constructor(ctx: Context, config: Config) {
    super(ctx, 'codex')
    this.bridgeUrl = safeBridgeBase(config.bridgeUrl)
    this.controlTimeoutMs = config.readyTimeoutMs
    this.coordinator = new CodexCoordinator(new WebSocketCodexBridgeTransport(this.bridgeUrl, {
      readyTimeoutMs: config.readyTimeoutMs,
      isolateTimeoutMs: config.isolateTimeoutMs,
      releaseTimeoutMs: config.releaseTimeoutMs,
    }), {
      interruptTimeoutMs: config.interruptTimeoutMs,
      flush: async session => await ctx.sessions.flush(session as Agent['session']),
    })
    // Resume/start recovery is itself a claimed maintenance phase. The
    // lifecycle event fires after the Agent is published but before its native
    // loop wakes, so an open codex/* tail cannot sit indefinitely until a new
    // prompt happens to arrive.
    ctx.on('agent/session-start', ({ agent }) => { this.scheduleAgentRecovery(agent) })
    // A service can be loaded after the Agent registry has already published
    // live agents (HMR/plugin ordering). Sweep those agents immediately so an
    // open durable codex tail cannot run a native step before this Host owns
    // its guard/recovery maintenance boundary.
    for (const agent of ctx.agents.list()) this.scheduleAgentRecovery(agent)
    // Cordis unloads services through fiber effects; an arbitrary `dispose()`
    // method is not lifecycle-owned. Keep the bridge/coordinator shutdown on
    // the service fiber so HMR/uninstall cannot leave active turns behind.
    ctx.effect(() => async () => { await this.closeResources() }, 'codex: close coordinator')
  }

  private scheduleAgentRecovery(agent: Agent): void {
    if (this.closing) return
    const target = agent as unknown as object
    if (this.recoveryAgents.has(target)) return
    this.recoveryAgents.add(target)
    const needsRecovery = this.coordinator.prepareAgentRecovery(agent)
    if (!needsRecovery && this.coordinator.isAgentBlocked(agent)) {
      // A previous execution/recovery poisoned this Agent. Do not claim a
      // fresh maintenance slot merely to discover the same permanent gate.
      this.recoveryAgents.delete(target)
      return
    }
    const retryLater = () => {
      const attempt = (this.recoveryAttempts.get(target) ?? 0) + 1
      this.recoveryAgents.delete(target)
      if (this.closing) return
      if (attempt > 6) return
      this.recoveryAttempts.set(target, attempt)
      const previousTimer = this.recoveryTimers.get(target)
      if (previousTimer !== undefined) clearTimeout(previousTimer)
      const timer = setTimeout(() => {
        this.recoveryTimers.delete(target)
        this.scheduleAgentRecovery(agent)
      }, Math.min(250 * attempt, 2_000))
      this.recoveryTimers.set(target, timer)
      timer.unref?.()
    }
    try {
      this.coordinator.ensureAgentGuard(agent)
      const maintenance = agent.runMaintenance(async signal => {
        if (signal.aborted) {
          retryLater()
          return
        }
        try {
          await this.coordinator.recover(agent.session)
          if (needsRecovery) this.coordinator.reconcileAgentRecovery(agent)
          this.recoveryAttempts.delete(target)
        } catch {
          // Recovery failure is a persistent pre-step quarantine. Returning
          // from maintenance lets the pinned Agent dispose cleanly; its own
          // guard rejects every native wake until reconciliation/close.
          await this.coordinator.holdRecovery(String(agent.session.id), signal, agent)
          // A busy/temporarily unavailable backend gets a bounded idle retry;
          // the Agent-owned poison remains active throughout the gap.
          retryLater()
        }
      })
      void maintenance.catch(() => retryLater())
    } catch {
      // A concurrent owner wins the maintenance claim; the next explicit
      // retry runs the idempotent bounded recovery gate once the Agent is idle.
      retryLater()
    }
  }

  /** Start one host-owned maintenance delegation after durable acceptance + bridge reserve. */
  @Remote('start')
  async start(agent: Agent, request: CodexStartRequest, signal: AbortSignal): Promise<CodexStartResult> {
    // Gate at source level before auth/status fetches as well as before the
    // coordinator. The bridge independently enforces the split-process
    // credential boundary before accepting the WebSocket turn.
    return await guardedCodexStart(codexTurnCapability(), async () => {
      const status = await this.authStatus(signal)
      if (!status.loggedIn || status.account === null) {
        throw Object.assign(new Error(status.message ?? 'ChatGPT login required'), { code: 'not_authenticated' })
      }
      return await this.coordinator.start(agent, request, signal)
    })
  }

  /** Record durable interrupt intent and await the authoritative bridge terminal. */
  @Remote('interrupt')
  async interrupt(agent: Agent, executionId: string, signal: AbortSignal): Promise<CodexInterruptResult> {
    signal.throwIfAborted()
    return await this.coordinator.interrupt(agent, executionId, 'barge-in')
  }

  /** One-shot allowlisted approval decision; read-only turns should not request writes. */
  @Remote('approvalDecision')
  async approvalDecision(agent: Agent, executionId: string, approvalId: string, decision: CodexApprovalDecision, signal: AbortSignal): Promise<CodexApprovalResult> {
    signal.throwIfAborted()
    return await this.coordinator.approvalDecision(agent, executionId, approvalId, decision)
  }

  /** Strict auth/capability status plus active execution ids, never raw errors. */
  @Remote('status')
  async status(agent: Agent, signal: AbortSignal): Promise<CodexRuntimeStatus> {
    const auth = await this.authStatus(signal)
    return { ...auth, executions: this.coordinator.status(String(agent.session.id)) }
  }

  /** Live App Server picker catalog, projected through the Host-only bridge. */
  @Remote('models')
  async models(agent: Agent, signal: AbortSignal): Promise<CodexModelCatalogResult> {
    void agent
    if (typeof fetch !== 'function') throw Object.assign(new Error('Codex bridge unavailable'), { code: 'bridge_unavailable' })
    try {
      const response = await this.fetchControl('/api/codex/models', { signal })
      if (!response.ok) throw Object.assign(new Error('Codex model catalog unavailable'), { code: 'bridge_unavailable' })
      const catalog = strictModelCatalog(await this.readControlJson(response))
      if (catalog === undefined) throw Object.assign(new Error('Codex model catalog is invalid'), { code: 'bridge_protocol' })
      return catalog
    } catch (error) {
      if (error instanceof Error && 'code' in error) throw error
      throw Object.assign(new Error('Codex model catalog unavailable'), { code: 'bridge_unavailable' })
    }
  }

  /** Typed browser-login start; auth URL is validated before crossing Host. */
  @Remote('loginStart')
  async loginStart(agent: Agent, signal: AbortSignal): Promise<CodexLoginStartResult> {
    if (this.closing) throw Object.assign(new Error('Codex service is closing'), { code: 'bridge_unavailable' })
    if (this.loginOwners.size !== 0 || this.loginStartInFlight || this.loginStartUncertain) {
      throw Object.assign(new Error('Codex login is already pending'), { code: 'turn_in_progress' })
    }
    signal.throwIfAborted()
    // Generate the operation before claiming the in-flight latch. A local
    // crypto/runtime failure must not strand the Host in a permanently busy
    // state or make a caller retry against an operation that was never sent.
    const operationId = createOperationId()
    this.loginStartInFlight = true
    let settleLoginStart!: () => void
    const loginStartSettled = new Promise<void>(resolve => { settleLoginStart = resolve })
    this.loginStartSettled = loginStartSettled
    let ownedLoginId: string | undefined
    try {
      // Once the POST is sent, do not let the caller's AbortSignal tear down
      // the transport before we learn the exact login id. If the UI aborts
      // during this window, cancel the exact owner through a fresh signal.
      let body: Record<string, unknown> | undefined
      let lastError: unknown
      for (let attempt = 0; attempt < 2 && body === undefined; attempt += 1) {
        try {
          const requestBody = JSON.stringify({ operation_id: operationId })
          if (new TextEncoder().encode(requestBody).byteLength > LOGIN_START_MAX_BODY_BYTES) {
            throw Object.assign(new Error('Codex login request is invalid'), { code: 'bridge_protocol' })
          }
          // No caller signal after the first send: the Host owns the bounded
          // retry/reconcile operation and cancels the exact loginId later.
          body = await this.loginFetch('/api/codex/auth/login/start', {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: requestBody,
          })
        } catch (error) {
          lastError = error
          const retryable = error instanceof Error && 'code' in error
            && (error as { code?: unknown; authoritative?: unknown }).code === 'bridge_unavailable'
            && (error as { authoritative?: unknown }).authoritative !== true
          if (!retryable || attempt === 1) throw error
        }
      }
      if (body === undefined) {
        if (lastError instanceof Error) throw lastError
        throw Object.assign(new Error('Codex login unavailable'), {
          code: 'bridge_unavailable',
          authoritative: false,
        })
      }
      const responseOperationId = safeOperationId(body['operation_id'])
      const loginId = safeLoginId(body['login_id'])
      const authUrl = safeAuthUrl(body['auth_url'])
      if (responseOperationId !== operationId || loginId === undefined || authUrl === undefined || body['status'] !== 'pending') {
        throw Object.assign(new Error('Codex login response is invalid'), {
          code: 'bridge_protocol',
          authoritative: true,
        })
      }
      ownedLoginId = loginId
      this.loginOwners.set(loginId, { sessionId: String(agent.session.id), agent })
      if (signal.aborted) {
        // Keep this callable from the small object-shaped test harnesses used
        // by the Host contract tests: the operation is owned by this class's
        // implementation, not by a dynamically supplied `this` method.
        await CodexService.prototype.cancelLoginAfterCallerAbort.call(this, agent, loginId)
        if (this.loginOwners.has(loginId)) this.loginStartUncertain = true
        signal.throwIfAborted()
      }
      return { loginId, status: 'pending', authUrl }
    } catch (error) {
      // A transport timeout/disconnect has no HTTP response and may have been
      // accepted remotely; without an exact id, preserve a fail-closed
      // quarantine instead of allowing a second pending flow. HTTP responses
      // (including 4xx/5xx) and protocol errors tied to a received response
      // are authoritative and must not be mislabeled as an unknown owner.
      const authoritative = error instanceof Error && 'authoritative' in error
        && (error as { authoritative?: unknown }).authoritative === true
      if (ownedLoginId === undefined && !authoritative) this.loginStartUncertain = true
      throw error
    } finally {
      if (!this.loginStartUncertain) {
        this.loginStartInFlight = false
      }
      settleLoginStart()
      if (this.loginStartSettled === loginStartSettled) this.loginStartSettled = undefined
    }
  }

  /** Typed login poll; raw backend messages never cross the Remote boundary. */
  @Remote('loginStatus')
  async loginStatus(agent: Agent, loginId: string, signal: AbortSignal): Promise<CodexLoginStatusResult> {
    const safeId = safeLoginId(loginId)
    if (safeId === undefined) throw Object.assign(new Error('Codex login id is invalid'), { code: 'invalid_request' })
    CodexService.prototype.assertLoginOwner.call(this, safeId, String(agent.session.id))
    const body = await this.loginFetch(`/api/codex/auth/login/${encodeURIComponent(safeId)}`, { method: 'GET', signal })
    const status = body['status']
    if (status !== 'pending' && status !== 'completed' && status !== 'failed' && status !== 'canceled' && status !== 'not_found') {
      throw Object.assign(new Error('Codex login status is invalid'), { code: 'bridge_protocol' })
    }
    const success = typeof body['success'] === 'boolean' ? body['success'] : undefined
    const result: CodexLoginStatusResult = success === undefined
      ? { loginId: safeId, status }
      : { loginId: safeId, status, success }
    if (status === 'completed' || status === 'failed' || status === 'canceled' || status === 'not_found') this.loginOwners.delete(safeId)
    return result
  }

  /**
   * Recover the one pending login owned by this session after a browser
   * reload. The login id is never searched or returned across sessions.
   */
  @Remote('loginPending')
  async loginPending(agent: Agent, signal: AbortSignal): Promise<CodexLoginStatusResult | null> {
    const sessionId = String(agent.session.id)
    const entry = [...this.loginOwners.entries()].find(([, owner]) => owner.sessionId === sessionId)
    if (entry === undefined) return null
    const [loginId] = entry
    const body = await this.loginFetch(`/api/codex/auth/login/${encodeURIComponent(loginId)}`, { method: 'GET', signal })
    const status = body['status']
    if (status !== 'pending' && status !== 'completed' && status !== 'failed' && status !== 'canceled' && status !== 'not_found') {
      throw Object.assign(new Error('Codex login status is invalid'), { code: 'bridge_protocol' })
    }
    const success = typeof body['success'] === 'boolean' ? body['success'] : undefined
    const result: CodexLoginStatusResult = success === undefined
      ? { loginId, status }
      : { loginId, status, success }
    if (status !== 'pending') this.loginOwners.delete(loginId)
    return result
  }

  /** Typed cancellation; no token or backend message is returned. */
  @Remote('loginCancel')
  async loginCancel(agent: Agent, loginId: string, signal: AbortSignal): Promise<CodexLoginCancelResult> {
    const safeId = safeLoginId(loginId)
    if (safeId === undefined) throw Object.assign(new Error('Codex login id is invalid'), { code: 'invalid_request' })
    CodexService.prototype.assertLoginOwner.call(this, safeId, String(agent.session.id))
    const body = await this.loginFetch(`/api/codex/auth/login/${encodeURIComponent(safeId)}/cancel`, { method: 'POST', signal })
    const status = body['status']
    if (status === 'completed' || status === 'failed' || status === 'not_found') {
      // The backend won the cancellation race. Release the exact owner and
      // report the authoritative terminal outcome; never manufacture a
      // `canceled` result for a terminal flow.
      this.loginOwners.delete(safeId)
      return { loginId: safeId, status, success: false }
    }
    if (status !== 'canceled') throw Object.assign(new Error('Codex login cancellation failed'), { code: 'bridge_protocol' })
    this.loginOwners.delete(safeId)
    return { loginId: safeId, status: 'canceled', success: false }
  }

  async dispose(): Promise<void> {
    await this.closeResources()
  }

  private async closeResources(): Promise<void> {
    if (this.closePromise !== undefined) return this.closePromise
    this.closing = true
    for (const timer of this.recoveryTimers.values()) clearTimeout(timer)
    this.recoveryTimers.clear()
    this.closePromise = (async () => {
      // A POST may already have been accepted while its response is still in
      // flight. Wait for the Host-owned operation to learn the exact login id
      // before canceling owners or closing the service resources.
      await this.loginStartSettled
      // Keep this prototype call compatible with the small object-shaped
      // Host contract harnesses; real instances have the same method.
      await CodexService.prototype.cancelPendingLoginsOnClose.call(this)
      // A terminal cancel response removes the exact owner.  Any transport
      // ambiguity deliberately leaves it in the process registry so the next
      // service instance can reconcile it; never clear this map on disposal.
      if (this.loginOwners.size !== 0) this.loginStartUncertain = true
      this.loginStartInFlight = this.loginStartUncertain
      await this.coordinator.close()
    })()
    return this.closePromise
  }

  private async cancelPendingLoginsOnClose(): Promise<void> {
    for (const [loginId, owner] of this.loginOwners) {
      if (owner.agent === undefined) {
        // An HMR-created service can still poll/cancel by session/login id,
        // but cannot manufacture the old Agent object for a typed call. Keep
        // the owner quarantined until the owning session is reattached.
        this.loginStartUncertain = true
        continue
      }
      try {
        await CodexService.prototype.loginCancel.call(
          this,
          owner.agent,
          loginId,
          new AbortController().signal,
        )
      } catch {
        // Canceled/completed/failed/not_found responses remove the owner in
        // loginCancel. A transport/protocol failure leaves it present and
        // therefore fail-closed for a later service instance.
        if (this.loginOwners.has(loginId)) this.loginStartUncertain = true
      }
    }
  }

  private assertLoginOwner(loginId: string, sessionId: string): void {
    const owner = this.loginOwners.get(loginId)
    if (owner === undefined || owner.sessionId !== sessionId) {
      throw Object.assign(new Error('Codex login request is not owned by this session'), { code: 'invalid_request' })
    }
  }

  private async cancelLoginAfterCallerAbort(agent: Agent, loginId: string): Promise<void> {
    try {
      await CodexService.prototype.loginCancel.call(this, agent, loginId, new AbortController().signal)
    } catch {
      // A terminal backend response also clears the exact owner. Any
      // transport failure leaves it retained and the Host stays blocked.
    }
  }

  private async authStatus(signal: AbortSignal): Promise<CodexStatus> {
    if (typeof fetch !== 'function') {
      return {
        capability: 'unavailable',
        loggedIn: false,
        requiresOpenAiAuth: true,
        account: null,
        loginUrl: LOGIN_URL,
        message: 'Codex bridge unavailable',
      }
    }
    try {
      const response = await this.fetchControl('/api/codex/auth', { signal })
      if (!response.ok) {
        return {
          capability: 'unavailable',
          loggedIn: false,
          requiresOpenAiAuth: true,
          account: null,
          loginUrl: LOGIN_URL,
          message: 'Codex bridge unavailable',
        }
      }
      const body = await this.readControlJson(response)
      const loggedIn = body['logged_in'] === true
      const requiresOpenAiAuth = body['requires_openai_auth'] === true
      const account = strictChatgptAccount(body['account'])
      const authenticated = loggedIn && account !== null
      const capability = codexTurnCapability()
      const base = {
        capability,
        loggedIn,
        requiresOpenAiAuth,
        account,
        loginUrl: LOGIN_URL,
      }
      if (!authenticated) return { ...base, message: 'ChatGPT login required' }
      if (capability === 'unavailable') return { ...base, message: 'Codex turn execution unavailable' }
      return base
    } catch {
      return {
        capability: 'unavailable',
        loggedIn: false,
        requiresOpenAiAuth: true,
        account: null,
        loginUrl: LOGIN_URL,
        message: 'Codex bridge unavailable',
      }
    }
  }

  private async loginFetch(path: string, init: RequestInit): Promise<Record<string, unknown>> {
    if (typeof fetch !== 'function') {
      throw Object.assign(new Error('Codex bridge unavailable'), { code: 'bridge_unavailable', authoritative: false })
    }
    try {
      const response = await this.fetchControl(path, init)
      if (!response.ok) {
        const code = response.status === 409
          ? 'turn_in_progress'
          : response.status >= 500
            ? 'bridge_unavailable'
            : 'invalid_request'
        throw Object.assign(new Error('Codex login request was rejected'), {
          code,
          authoritative: true,
        })
      }
      try {
        return await this.readControlJson(response)
      } catch (error) {
        // The server did answer; malformed/oversized JSON is a protocol
        // failure, not an ambiguous network outcome. Preserve that fact so a
        // login-start caller does not retry or poison the owner latch.
        if (error instanceof Error) {
          Object.assign(error, { authoritative: true })
          throw error
        }
        throw Object.assign(new Error('Codex bridge response is invalid'), {
          code: 'bridge_protocol',
          authoritative: true,
        })
      }
    } catch (error) {
      if (error instanceof Error && 'code' in error) throw error
      throw Object.assign(new Error('Codex bridge unavailable'), {
        code: 'bridge_unavailable',
        authoritative: false,
      })
    }
  }

  private async fetchControl(path: string, init: RequestInit): Promise<Response> {
    if (typeof fetch !== 'function') throw Object.assign(new Error('Codex bridge unavailable'), { code: 'bridge_unavailable' })
    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), this.controlTimeoutMs)
    const onAbort = () => controller.abort()
    if (init.signal?.aborted) controller.abort()
    init.signal?.addEventListener('abort', onAbort, { once: true })
    try {
      return await fetch(`${this.bridgeUrl}${path}`, { ...init, signal: controller.signal })
    } finally {
      clearTimeout(timeout)
      init.signal?.removeEventListener('abort', onAbort)
    }
  }

  private async readControlJson(response: Response): Promise<Record<string, unknown>> {
    const contentLength = response.headers.get('content-length')
    if (contentLength !== null && (!/^\d+$/.test(contentLength) || Number(contentLength) > MAX_CONTROL_RESPONSE_BYTES)) {
      throw Object.assign(new Error('Codex bridge response is too large'), { code: 'bridge_protocol' })
    }
    const raw = await this.readControlText(response)
    let parsed: unknown
    try { parsed = JSON.parse(raw) } catch {
      throw Object.assign(new Error('Codex bridge response is invalid'), { code: 'bridge_protocol' })
    }
    const object = objectOf(parsed)
    if (object === undefined) throw Object.assign(new Error('Codex bridge response is invalid'), { code: 'bridge_protocol' })
    return object
  }

  private async readControlText(response: Response): Promise<string> {
    const reader = response.body?.getReader()
    if (reader === undefined) {
      const raw = await response.text()
      if (raw.length > MAX_CONTROL_RESPONSE_BYTES) throw Object.assign(new Error('Codex bridge response is too large'), { code: 'bridge_protocol' })
      return raw
    }
    const chunks: Uint8Array[] = []
    let total = 0
    try {
      while (true) {
        const next = await reader.read()
        if (next.done) break
        const chunk = next.value
        total += chunk.byteLength
        if (total > MAX_CONTROL_RESPONSE_BYTES) {
          await reader.cancel()
          throw Object.assign(new Error('Codex bridge response is too large'), { code: 'bridge_protocol' })
        }
        chunks.push(chunk)
      }
    } finally {
      reader.releaseLock()
    }
    const bytes = new Uint8Array(total)
    let offset = 0
    for (const chunk of chunks) {
      bytes.set(chunk, offset)
      offset += chunk.byteLength
    }
    return new TextDecoder().decode(bytes)
  }
}

export default CodexService
