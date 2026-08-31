/** Exact Codex execution ownership for late-start/session-reuse cleanup. */

export interface CodexOwner {
  readonly sessionId: string
  readonly executionId: string
}

export type CodexOwnerRelease = (owner: CodexOwner) => Promise<void> | void

export const MAX_QUARANTINED_CODEX_OWNERS = 32

function ownerKey(owner: CodexOwner): string {
  return JSON.stringify([owner.sessionId, owner.executionId])
}

interface Entry {
  readonly owner: CodexOwner
  status: 'blocked' | 'releasing'
  promise: Promise<boolean> | undefined
}

/**
 * Keeps an exact remote owner alive until the Host acknowledges its release.
 *
 * The map is deliberately instance-scoped: an apply/session renderer owns one
 * quarantine and no component can accidentally release another session's
 * execution. Calling release twice while an attempt is in flight returns the
 * same Promise. A failed attempt remains in the map and can be retried by the
 * next lifecycle edge; it is never silently replaced by a new session owner.
 */
export class CodexOwnerQuarantine {
  private readonly entries = new Map<string, Entry>()

  quarantine(owner: CodexOwner): boolean {
    const key = ownerKey(owner)
    if (this.entries.has(key)) return true
    if (this.entries.size >= MAX_QUARANTINED_CODEX_OWNERS) return false
    this.entries.set(key, { owner: Object.freeze({ ...owner }), status: 'blocked', promise: undefined })
    return true
  }

  release(owner: CodexOwner, release: CodexOwnerRelease): Promise<boolean> {
    const key = ownerKey(owner)
    let entry = this.entries.get(key)
    if (entry === undefined) {
      if (!this.quarantine(owner)) return Promise.resolve(false)
      entry = this.entries.get(key)
      if (entry === undefined) return Promise.resolve(false)
    }
    if (entry.promise !== undefined) return entry.promise
    entry.status = 'releasing'
    const attempt = Promise.resolve()
      .then(() => release(entry!.owner))
      .then(() => {
        if (this.entries.get(key) === entry) this.entries.delete(key)
        return true
      })
      .catch(() => {
        if (this.entries.get(key) === entry) entry!.status = 'blocked'
        return false
      })
    let shared!: Promise<boolean>
    shared = attempt.finally(() => {
      if (this.entries.get(key) === entry && entry!.promise === shared) entry!.promise = undefined
    })
    entry.promise = shared
    return shared
  }

  async retryAll(release: CodexOwnerRelease): Promise<number> {
    let released = 0
    for (const entry of this.entries.values()) {
      if (await this.release(entry.owner, release)) released += 1
    }
    return released
  }

  has(owner: CodexOwner): boolean {
    return this.entries.has(ownerKey(owner))
  }

  isBlocked(owner: CodexOwner): boolean {
    const entry = this.entries.get(ownerKey(owner))
    return entry?.status === 'blocked'
  }

  owners(): readonly CodexOwner[] {
    return [...this.entries.values()].map(entry => entry.owner)
  }

  get size(): number {
    return this.entries.size
  }
}
