/** Bounded owner map for session-local renderer/operation resources. */
export function sessionOwnerKey(sessionId: string | number | undefined): string {
  return sessionId === undefined ? '__unbound__' : String(sessionId)
}

export class SessionOwnerMap<T> {
  private readonly owners = new Map<string, T>()
  private readonly mounts = new Map<string, number>()

  constructor(
    private readonly create: (owner: string) => T,
    private readonly dispose?: (owner: T, key: string) => void,
  ) {}

  get(sessionId: string | number | undefined): T {
    const key = sessionOwnerKey(sessionId)
    const existing = this.owners.get(key)
    if (existing !== undefined) return existing
    const created = this.create(key)
    this.owners.set(key, created)
    return created
  }

  /** Retain an owner for one mounted session renderer. */
  acquire(sessionId: string | number | undefined): { readonly owner: T; readonly release: () => void } {
    const key = sessionOwnerKey(sessionId)
    const owner = this.get(sessionId)
    this.mounts.set(key, (this.mounts.get(key) ?? 0) + 1)
    let released = false
    return {
      owner,
      release: () => {
        if (released) return
        released = true
        const count = (this.mounts.get(key) ?? 1) - 1
        if (count > 0) {
          this.mounts.set(key, count)
          return
        }
        this.mounts.delete(key)
        this.owners.delete(key)
        this.dispose?.(owner, key)
      },
    }
  }

  mountCount(sessionId: string | number | undefined): number {
    return this.mounts.get(sessionOwnerKey(sessionId)) ?? 0
  }

  values(): IterableIterator<T> {
    return this.owners.values()
  }

  get size(): number {
    return this.owners.size
  }

  clear(dispose?: (owner: T) => void): void {
    if (dispose !== undefined) {
      for (const owner of this.owners.values()) dispose(owner)
    }
    this.owners.clear()
    this.mounts.clear()
  }
}
