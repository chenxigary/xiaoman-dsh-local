/** Per-component cancellation fence for one mic stream's utterance queue. */
export class UtteranceGeneration {
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

  /** Advance the fence and abort every request owned by the old generation. */
  cancel(): number {
    this.value += 1
    for (const controller of this.pending) controller.abort()
    this.pending.clear()
    return this.value
  }
}
