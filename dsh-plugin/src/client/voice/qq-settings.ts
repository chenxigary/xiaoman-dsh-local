/** Browser-only QQ push preference; absence and storage failures are off. */

export const QQ_PUSH_KEY = 's2s.voice.qqPush'

export interface QqPushStorage {
  getItem: (key: string) => string | null
  setItem: (key: string, value: string) => void
}

export function readQqPush(storage: QqPushStorage | undefined = typeof localStorage === 'undefined' ? undefined : localStorage): boolean {
  try {
    return storage?.getItem(QQ_PUSH_KEY) === '1'
  } catch {
    return false
  }
}

export function writeQqPush(enabled: boolean, storage: QqPushStorage | undefined = typeof localStorage === 'undefined' ? undefined : localStorage): void {
  try {
    storage?.setItem(QQ_PUSH_KEY, enabled ? '1' : '0')
  } catch {
    // Browser storage is optional; the in-memory toggle remains authoritative.
  }
}
