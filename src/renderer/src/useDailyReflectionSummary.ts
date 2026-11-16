import { useEffect, useRef, useState } from 'react'
import { localApi } from './api'
import { getCompanionVoiceState, subscribeCompanionVoice } from './companionVoice'

export function useDailyReflectionSummary(options: {
  sessionId: string; childId: string; enabled: boolean; busy: boolean
  onSummary: (message: string) => void
}): void {
  const callback = useRef(options.onSummary)
  callback.current = options.onSummary
  const [message, setMessage] = useState<{ sessionId: string; text: string } | null>(null)
  const spoken = useRef<string | null>(null)

  useEffect(() => {
    if (!options.enabled || spoken.current === options.sessionId) return
    let current = true
    setMessage(null)
    void localApi.dailyLearningSummary(options.childId).then((summary) => {
      if (!current) return
      if (summary.child_id !== options.childId || summary.cloud_transmission_allowed !== false || typeof summary.child_response !== 'string') throw new Error('INVALID_SUMMARY_RECEIPT')
      setMessage({ sessionId: options.sessionId, text: summary.child_response })
    }).catch(() => {
      if (current) setMessage({ sessionId: options.sessionId, text: '今天的总结暂时没有读到，原有记录还在。你可以告诉我哪里还不确定，或说“总结今天的学习”再试一次。' })
    })
    return () => { current = false }
  }, [options.enabled, options.sessionId, options.childId])

  useEffect(() => {
    if (!options.enabled || options.busy || !message || message.sessionId !== options.sessionId || spoken.current === options.sessionId) return
    let timer: ReturnType<typeof setTimeout> | undefined
    const schedule = (): void => {
      clearTimeout(timer)
      if (['speaking', 'preparing'].includes(getCompanionVoiceState()) || spoken.current === options.sessionId) return
      timer = setTimeout(() => {
        spoken.current = options.sessionId
        callback.current(message.text)
      }, 650)
    }
    const unsubscribe = subscribeCompanionVoice(schedule)
    schedule()
    return () => { clearTimeout(timer); unsubscribe() }
  }, [options.enabled, options.busy, options.sessionId, message])
}
