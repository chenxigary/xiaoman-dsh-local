/** Media playback policy kept separate from the React view for regression tests. */

/** A single idle clip must loop; multiple clips advance through `ended`. */
export function shouldLoopIdleVideo(videoCount: number): boolean {
  return videoCount === 1
}

/** WebRTC must keep advancing while visible; otherwise reveal fallback/reconnect. */
export function avatarPlaybackIsStalled(
  previousTime: number,
  currentTime: number,
  paused: boolean,
  readyState: number,
): boolean {
  if (paused || readyState < 2) return true
  if (!Number.isFinite(previousTime) || !Number.isFinite(currentTime)) return true
  return currentTime - previousTime < 0.5
}
