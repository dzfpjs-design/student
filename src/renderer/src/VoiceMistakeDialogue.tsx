import { useQuestionViewportFocus } from './useQuestionViewportFocus'
import { privacyStopPending } from './deviceCommandReceipt'
import { useEffect, useRef, useState } from 'react'
import { BookOpen, LoaderCircle } from 'lucide-react'
import { ApiError, localApi } from './api'
import type { MistakeDialogue } from './types'
import { releaseVoiceQuestionCapture, setVoiceQuestionCapture, voiceQuestionCaptureEpoch } from './voiceCaptureContext'
import { CAPTURE_SAVED_EVENT, type CaptureReceipt } from './deviceCommandReceipt'

const phases: Record<string, string> = {
  candidate: '核对题目', answer: '回忆原答案', confirm: '核对原答案', diagnosis: '找出卡点',
  remediation: '一起订正', attempt: '独立试一题', scheduled: '后续复习', paused: '已保留进度', closed: '本页已整理'
}

type DialogueProps = { sessionId: string; childId: string; artifactId?: string; mistakeId?: string;
  onOpened?: (handled: boolean, dialogue?: MistakeDialogue) => void; openSignal?: AbortSignal }

export function VoiceMistakeDialogue(props: DialogueProps): React.JSX.Element {
  return <OwnedMistakeDialogue key={`${props.sessionId}:${props.childId}:${props.artifactId ?? props.mistakeId}`} {...props} />
}

function OwnedMistakeDialogue({ sessionId, childId, artifactId, mistakeId, onOpened, openSignal }: DialogueProps): React.JSX.Element {
  const [dialogue, setDialogue] = useState<MistakeDialogue | null>(null)
  const [error, setError] = useState<string | null>(null)
  const questionRef = useQuestionViewportFocus<HTMLParagraphElement>(
    dialogue?.state.question_text && !['paused', 'closed', 'scheduled'].includes(dialogue.phase)
      ? `${childId}:${sessionId}:${dialogue.mistake_id ?? dialogue.candidate_id}:${dialogue.phase}:${dialogue.answer_binding?.item_id ?? dialogue.dialogue_binding?.item_id ?? dialogue.state.question_text}:${dialogue.answer_binding?.source_version ?? dialogue.dialogue_binding?.source_version ?? ''}`
      : null,
    () => !privacyStopPending(sessionId))
  const actionRef = useRef(crypto.randomUUID())
  const openingRef = useRef({ onOpened, signal: openSignal })
  useEffect(() => {
    let cancelled = false
    let revision = 0
    let openedSuccessfully = false
    let inFlight: Promise<MistakeDialogue | null> | null = null
    let ownedEpoch: number | null = null
    const actionId = actionRef.current
    const release = (): void => {
      if (ownedEpoch !== null) releaseVoiceQuestionCapture(sessionId, childId, ownedEpoch)
      ownedEpoch = null
    }
    const matches = (value: MistakeDialogue): boolean => value.session_id === sessionId && value.child_id === childId
      && (artifactId ? value.state?.artifact_id === artifactId
        : Boolean(mistakeId && value.mistake_id === mistakeId && !value.state?.artifact_id))
      && [value.answer_binding, value.dialogue_binding].every(binding => !binding || (
        binding.session_id === sessionId && binding.child_id === childId
        && binding.mistake_id === value.mistake_id && binding.phase === value.phase
        && (binding.mistake_id !== null || ('candidate_id' in binding
          && binding.candidate_id === value.candidate_id && binding.artifact_id === artifactId))))
    const bind = (value: MistakeDialogue): void => {
      const binding = value.answer_binding ?? value.dialogue_binding
      setVoiceQuestionCapture(sessionId, childId, binding ? { kind: 'mistake', binding } : null)
      ownedEpoch = voiceQuestionCaptureEpoch(sessionId)
    }
    setDialogue(null); setError(null)
    const start = (signal?: AbortSignal): Promise<MistakeDialogue | null> => {
      if (inFlight) return inFlight
      release()
      const requestedRevision = ++revision
      const captureEpoch = voiceQuestionCaptureEpoch(sessionId)
      const current = (): boolean => !cancelled && !signal?.aborted && requestedRevision === revision && captureEpoch === voiceQuestionCaptureEpoch(sessionId)
      const run = async (): Promise<MistakeDialogue | null> => { try {
        if (artifactId) await localApi.beginMistakeDialogue(sessionId, artifactId, actionId)
        if (!current()) {
          if (!cancelled && requestedRevision === revision) setError('当前提问已变化，原题记录保留，不会替换新的学习任务。')
          return null
        }
        // An idempotent begin can replay an old response after the child has progressed.
        const value = (await localApi.mistakeDialogue(sessionId)).dialogue
        if (!current()) {
          if (!cancelled && requestedRevision === revision) setError('当前提问已变化，原题记录保留，不会替换新的学习任务。')
          return null
        }
        if (!value || !matches(value)) throw new Error('原页与当前学习记录尚未核对一致，不会沿用其他题目。请说“重试整理错题”。')
        openedSuccessfully = true
        bind(value)
        setDialogue(value); setError(null)
        if (artifactId) window.dispatchEvent(new CustomEvent('hsc-agent-intervention', { detail: { message: value.response_text, sessionId, childId } }))
        return value
      } catch (cause) {
        if (!current()) return null
        const message = cause instanceof ApiError ? cause.message : '题目还没核对成功。请说“重试整理错题”，我会从保存的位置继续。'
        setError(message)
        if (artifactId) window.dispatchEvent(new CustomEvent('hsc-agent-intervention', { detail: { message, sessionId, childId } }))
        return null
      } }
      inFlight = run().finally(() => { inFlight = null })
      return inFlight
    }
    const update = (event: Event): void => {
      const value = (event as CustomEvent<MistakeDialogue>).detail
      if (value && matches(value)) {
        openedSuccessfully = true
        revision++; setDialogue(value); setError(null)
        bind(value)
      }
    }
    const retry = (event: Event): void => {
      const detail = (event as CustomEvent<{ action?: string; sessionId?: string; childId?: string; signal?: AbortSignal;
        report?: (value: MistakeDialogue) => void; resolve?: (handled: boolean) => void }>).detail
      if (!artifactId || event.defaultPrevented || detail?.action !== 'open' || detail.sessionId !== sessionId || detail.childId !== childId
        || detail.signal?.aborted) return
      event.preventDefault()
      const abort = (): void => { revision++; if (!cancelled) setError('整理已暂停，原题记录保留。') }
      detail.signal?.addEventListener('abort', abort, { once: true })
      void start(detail.signal).then(value => {
        if (detail.signal?.aborted) return
        if (value) detail.report?.(value)
        detail.resolve?.(Boolean(value))
      })
        .finally(() => detail.signal?.removeEventListener('abort', abort))
    }
    const captured = (event: Event): void => {
      const value = (event as CustomEvent<CaptureReceipt>).detail
      if (artifactId && !openedSuccessfully && value?.sessionId === sessionId && value.artifactId === artifactId && value.status === 'recognized') void start()
    }
    void start(openingRef.current.signal).then(value => {
      if (value) openingRef.current.onOpened?.(true, value)
      else openingRef.current.onOpened?.(false)
    })
    window.addEventListener('hsc-mistake-dialogue-updated', update)
    window.addEventListener('hsc-mistake-capture-voice-action', retry)
    window.addEventListener(CAPTURE_SAVED_EVENT, captured)
    return () => {
      cancelled = true
      release()
      window.removeEventListener('hsc-mistake-dialogue-updated', update)
      window.removeEventListener('hsc-mistake-capture-voice-action', retry)
      window.removeEventListener(CAPTURE_SAVED_EVENT, captured)
    }
  }, [sessionId, childId, artifactId, mistakeId])
  return <section className="mistake-capture-composer" aria-label="错题语音订正">
    <header><BookOpen size={20} /><strong>{dialogue ? phases[dialogue.phase] ?? '错题订正' : '正在整理题目'}</strong></header>
    {!dialogue && !error && <LoaderCircle size={20} aria-label="正在加载" />}
    {dialogue?.state.question_text && <p ref={questionRef} data-question-viewport="mistake-dialogue" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{dialogue.state.question_text}</p>}
    {dialogue?.state.submitted_answer && <p>当时的答案：{dialogue.state.submitted_answer}</p>}
    {dialogue?.state.knowledge_title && <p className="muted-copy">{dialogue.state.knowledge_title}</p>}
    {dialogue && <p role="status">{dialogue.response_text}</p>}
    {error && <p role="alert">{error}</p>}
  </section>
}
