/** Character values and the verified avatar fallback policy. */

/** Character namespaces accepted by the bridge and the Codex host. */
export type CharacterId = 'default' | 'xiaoman'

/** Companion lifecycle states used by renderer hooks. */
export type AvatarState = 'idle' | 'listening' | 'thinking' | 'speaking'

/**
 * Resolve a state asset while preserving the verified idle fallback. Callers
 * use the returned idle candidate when a state-specific asset is absent.
 * @param character - selected character namespace.
 * @param state - desired lifecycle state.
 * @param media - asset lists keyed by character or character/state.
 * @returns a copied candidate list.
 */
export function avatarStateCandidates(
  character: CharacterId,
  state: AvatarState,
  media: Readonly<Record<string, string[]>>,
): string[] {
  const characterMedia = media[character] ?? []
  const stateMedia = media[`${character}:${state}`] ?? characterMedia
  if (stateMedia.length > 0) return [...stateMedia]
  if (state !== 'idle') {
    const idle = media[`${character}:idle`] ?? media.default ?? []
    if (idle.length > 0) return [...idle]
  }
  return []
}
