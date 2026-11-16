import type { CompanionSpeechOptions, CompanionSpeechResult } from './companionVoice'
import type { TutorBlackboardPresentation } from './types'

type Speak = (text: string, sessionId: string, onResult: (result: CompanionSpeechResult) => void,
  onStart: () => void, options: CompanionSpeechOptions) => void

/** Delivery acknowledgement is not audio completion, especially with OS voices. */
export function playTutorSegments(values: {
  presentation: TutorBlackboardPresentation; sessionId: string; speak: Speak;
  options: CompanionSpeechOptions; isCurrent: () => boolean;
  onReveal: (count: number) => void; onDelivery?: (result: CompanionSpeechResult) => void;
  onComplete?: () => void; startIndex?: number;
}): void {
  const { presentation, sessionId, speak, options, isCurrent, onReveal, onDelivery } = values
  const play = (index: number): void => {
    if (!isCurrent()) return
    if (index >= presentation.segments.length) { values.onComplete?.(); return }
    let started = false
    let ended = false
    speak(presentation.segments[index].narration, sessionId, result => {
      if (!isCurrent()) return
      onDelivery?.(result)
      // A failed voice reveals only this segment, never the entire solution.
      if (result === 'text_only' && !started) onReveal(index + 1)
    }, () => {
      if (!isCurrent() || started || ended) return
      started = true
      onReveal(index + 1)
    }, { ...options, onPlaybackEnd: () => {
      if (!isCurrent() || !started || ended) return
      ended = true
      queueMicrotask(() => play(index + 1))
    } })
  }
  play(Math.max(0, Math.min(presentation.segments.length, values.startIndex ?? 0)))
}
