/** Tokenized renderer operations for one apply-level session owner. */
export class SessionOperationOwner {
  private readonly tts = new Map<symbol, AbortController>()
  private readonly interrupts = new Map<symbol, () => void>()
  private readonly turnCancels = new Map<symbol, () => void>()

  registerTts(token: symbol, controller: AbortController | null): void {
    if (controller === null) {
      this.tts.get(token)?.abort()
      this.tts.delete(token)
    }
    else this.tts.set(token, controller)
  }

  registerInterrupt(token: symbol, handler: (() => void) | null): void {
    if (handler === null) this.interrupts.delete(token)
    else this.interrupts.set(token, handler)
  }

  registerTurnCancel(token: symbol, handler: (() => void) | null): void {
    if (handler === null) this.turnCancels.delete(token)
    else this.turnCancels.set(token, handler)
  }

  abortTts(token?: symbol): void {
    if (token !== undefined) {
      this.tts.get(token)?.abort()
      this.tts.delete(token)
      return
    }
    for (const controller of this.tts.values()) controller.abort()
    this.tts.clear()
  }

  cancelTurns(): void {
    for (const handler of this.turnCancels.values()) handler()
  }

  interrupt(): void {
    for (const handler of this.interrupts.values()) handler()
  }

  dispose(): void {
    this.abortTts()
    this.cancelTurns()
    this.turnCancels.clear()
    this.interrupts.clear()
  }

  get counts(): { readonly tts: number; readonly interrupts: number; readonly turnCancels: number } {
    return {
      tts: this.tts.size,
      interrupts: this.interrupts.size,
      turnCancels: this.turnCancels.size,
    }
  }
}
