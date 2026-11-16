import { useCallback, useRef, useState } from 'react'
import { idleRealtimeVoiceStatus, type RealtimeVoiceStatus } from './realtimeVoiceRuntime'

// Keep non-rendering consumers current on every notification, while only
// scheduling a workspace render when a status field actually changes.
export function useRealtimeVoiceStatus() {
  const [status, setStatus] = useState<RealtimeVoiceStatus>(idleRealtimeVoiceStatus)
  const current = useRef(status)
  const update = useCallback((next: RealtimeVoiceStatus) => {
    const previous = current.current
    current.current = next
    if (previous.runtime !== next.runtime || previous.phase !== next.phase
      || previous.speechActive !== next.speechActive || previous.capturePending !== next.capturePending
      || previous.captureReady !== next.captureReady || previous.pendingUtterances !== next.pendingUtterances
      || previous.recoverable !== next.recoverable || previous.detail !== next.detail) setStatus(next)
  }, [])
  return { status, current, update }
}
