import { useSyncExternalStore } from 'react'

export type VoiceInputMode = 'manual' | 'hands-free'

const prefix = 'hsc-voice-input-mode:'
const volatile = new Map<string, VoiceInputMode>()
const listeners = new Set<() => void>()

export function readVoiceInputMode(childId: string): VoiceInputMode {
  const temporary = volatile.get(childId)
  if (temporary) return temporary
  try { return window.localStorage.getItem(prefix + childId) === 'hands-free' ? 'hands-free' : 'manual' }
  catch { return 'manual' }
}

export function saveVoiceInputMode(childId: string, mode: VoiceInputMode): boolean {
  let saved = true
  try {
    window.localStorage.setItem(prefix + childId, mode)
    volatile.delete(childId)
  } catch {
    volatile.set(childId, mode)
    saved = false
  }
  listeners.forEach(notify => notify())
  return saved
}

function subscribe(notify: () => void): () => void {
  const storage = (event: StorageEvent): void => {
    if (!event.key || event.key.startsWith(prefix)) notify()
  }
  listeners.add(notify)
  window.addEventListener('storage', storage)
  return () => {
    listeners.delete(notify)
    window.removeEventListener('storage', storage)
  }
}

export function useVoiceInputMode(childId: string): VoiceInputMode {
  return useSyncExternalStore(subscribe, () => readVoiceInputMode(childId), () => 'manual')
}
