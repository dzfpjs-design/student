import { useEffect, useState } from 'react'

type Session = { session_id: string; status: string; state: string }
export const LEARNING_AWAKE_WARNING = '学习期间的防休眠保护未能确认，电脑可能自动休眠。'

export function useLearningSessionAwake(session: Session): string | null {
  const [warning, setWarning] = useState<string | null>(null)
  const active = session.status === 'active' && session.state !== 'completed'
  useEffect(() => {
    setWarning(null)
    if (!active) return
    let leaseId: string | undefined
    // All operations are synchronous local IPC. No pending acquisition can
    // reappear after cleanup, including React StrictMode's mount/cleanup cycle.
    let desktop: typeof window.desktop
    try {
      desktop = window.desktop
      leaseId = crypto.randomUUID()
      const value = desktop?.setLearningSessionAwake(session.session_id, leaseId, true)
      if (!value?.active || !value.held) throw Error('RUNTIME_AWAKE_UNCONFIRMED')
    } catch { setWarning(LEARNING_AWAKE_WARNING) }
    return () => {
      try { if (leaseId) desktop?.setLearningSessionAwake(session.session_id, leaseId, false) } catch { /* Main records the finite failure; the view is leaving. */ }
    }
  }, [session.session_id, active])
  return warning
}
