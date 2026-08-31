/** Generation fence for native and Codex reply TTS chains. */
export class ReplyTtsGeneration {
  private value = 0
  private readonly pending = new Set<AbortController>()

  get current(): number {
    return this.value
  }

  isCurrent(generation: number): boolean {
    return generation === this.value
  }

  track(controller: AbortController): void {
    this.pending.add(controller)
  }

  untrack(controller: AbortController): void {
    this.pending.delete(controller)
  }

  /** Advance after barge-in or a new answer node, aborting old TTS work. */
  advance(): number {
    this.value += 1
    for (const controller of this.pending) controller.abort()
    this.pending.clear()
    return this.value
  }
}

/**
 * Exact durable execution fence for a shared speaker. A node key is a view
 * identity; this fence is the protocol identity that must stop old accepted
 * audio when a new Codex execution starts.
 */
export class ReplyExecutionFence {
  private executionId: string | null = null
  private value = 0

  begin(executionId: string): { readonly changed: boolean; readonly generation: number } {
    if (this.executionId === executionId) return { changed: false, generation: this.value }
    this.executionId = executionId
    this.value += 1
    return { changed: true, generation: this.value }
  }

  reset(): number {
    this.executionId = null
    this.value += 1
    return this.value
  }

  isCurrent(executionId: string, generation: number): boolean {
    return this.executionId === executionId && this.value === generation
  }

  get current(): string | null {
    return this.executionId
  }
}
