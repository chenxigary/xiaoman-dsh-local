/** Session-local authority switch between WebRTC Avatar audio and local PCM. */
export interface AvatarAudioRoutePort {
  readonly remote: boolean
  setRemote(value: boolean): void
  onChange(listener: () => void): () => void
}

export class AvatarAudioRoute implements AvatarAudioRoutePort {
  private value = false
  private listeners = new Set<() => void>()

  get remote(): boolean {
    return this.value
  }

  setRemote(value: boolean): void {
    if (this.value === value) return
    this.value = value
    for (const listener of this.listeners) listener()
  }

  onChange(listener: () => void): () => void {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }

  dispose(): void {
    this.value = false
    this.listeners.clear()
  }
}
