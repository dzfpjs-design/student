import {useEffect, useRef, useState} from 'react'
import {validVoiceRuntimePolicy, type VoiceRuntimePolicy} from '../../shared/voiceRuntimePolicy'

const retryDelays = [1000, 2000, 4000] as const

/** Bounded fresh reads only; failed or retired owners never enable capture. */
export function useVoiceRuntimePolicy(childId: string, sessionId: string, refresh = false): VoiceRuntimePolicy | null {
  const [current, setCurrent] = useState<{childId: string; sessionId: string; policy: VoiceRuntimePolicy} | null>(null)
  // Retain one physical read across a same-component child/session rebind.
  // IPC cannot be cancelled; its late reply only releases this slot.
  const inFlight = useRef<Promise<void> | null>(null)
  const queued = useRef<(() => void) | null>(null)
  useEffect(() => {
    let live = true, sequence = 0, retries = 0
    let timer: number | null = null
    const clearTimer = (): void => {
      if (timer !== null) window.clearTimeout(timer)
      timer = null
    }
    const read = (): void => {
      if (!live) return
      if (inFlight.current) { queued.current = read; return }
      queued.current = null
      const request = sequence
      let accepted = false
      const work = (async (): Promise<void> => {
        try {
          const desktop = window.desktop
          if (!desktop?.getVoiceRuntimePolicy) return
          const before = await desktop.getRuntimeConfig()
          if (before.status !== 'ready' || !Number.isSafeInteger(before.runtimeEpoch) || before.runtimeEpoch! <= 0) return
          const policy = await desktop.getVoiceRuntimePolicy()
          const after = await desktop.getRuntimeConfig()
          if (!live || request !== sequence || window.desktop !== desktop || after.status !== 'ready'
            || before.runtimeEpoch !== after.runtimeEpoch || before.localApiUrl !== after.localApiUrl
            || before.sessionToken !== after.sessionToken || !validVoiceRuntimePolicy(policy, after.runtimeEpoch)) return
          setCurrent({childId, sessionId, policy})
          accepted = policy.capture !== 'blocked'
        } catch { /* An unknown fresh read leaves capture blocked. */ }
        finally { if (live && request === sequence && !accepted) setCurrent(null) }
      })()
      inFlight.current = work
      const finished = (): void => {
        if (inFlight.current !== work) return
        inFlight.current = null
        const next = queued.current
        queued.current = null
        if (next) { next(); return }
        if (!live || request !== sequence) return
        if (!refresh && (accepted || retries === retryDelays.length)) return
        const delay = refresh ? 2000 : retryDelays[retries++]
        timer = window.setTimeout(() => { timer = null; read() }, delay)
      }
      void work.then(finished, finished)
    }
    const restart = (): void => {
      sequence++
      retries = 0
      clearTimer()
      setCurrent(null)
      read()
    }
    window.addEventListener('hsc-runtime-restarted', restart)
    restart()
    return () => {
      live = false
      sequence++
      clearTimer()
      if (queued.current === read) queued.current = null
      window.removeEventListener('hsc-runtime-restarted', restart)
    }
  }, [childId, sessionId, refresh])
  return current?.childId === childId && current.sessionId === sessionId ? current.policy : null
}
