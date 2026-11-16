import { localApi } from './api'
import type { CompanionSpeechOptions } from './companionVoice'
import type { RealtimeVoiceStatus } from './realtimeVoiceRuntime'
import type { DiagnosticPromptResult } from './types'
import { originalAudioCaptureRequired, subscribeVoiceCaptureContext, voiceCaptureReference } from './voiceCaptureContext'

type Owner = { childId: string; ready: boolean; activate?: () => void }
const owners = new Map<string, Owner>()
const listeners = new Set<() => void>()
const notify = (): void => { for (const listener of listeners) listener() }

// This is a transient microphone lease, never authority to score or retain audio.
export function registerOriginalAudioCaptureReadiness(sessionId: string, childId: string, activate?: () => void) {
  const owner: Owner = { childId, ready: false, activate }
  owners.set(sessionId, owner); notify()
  return {
    update(status: RealtimeVoiceStatus, busy: boolean): void {
      if (owners.get(sessionId) !== owner) return
      owner.ready = !busy && status.runtime === 'segmented_local' && status.phase === 'listening'
        && status.captureReady === true && status.speechActive === false
        && !status.capturePending && status.pendingUtterances === 0
      notify()
    },
    dispose(): void {
      if (owners.get(sessionId) !== owner) return
      owners.delete(sessionId); notify()
    },
  }
}

function samePrompt(result: DiagnosticPromptResult, sessionId: string,
  expected: Pick<NonNullable<DiagnosticPromptResult['prompt']>, 'child_id' | 'prompt_id' | 'item_version'>): boolean {
  const prompt = result.prompt
  return result.status === 'waiting_answer' && !result.reason && prompt?.origin_session_id === sessionId
    && prompt.child_id === expected.child_id && prompt.prompt_id === expected.prompt_id
    && prompt.item_version === expected.item_version && prompt.item?.track === 'pet'
    && prompt.item.assessment_mode === 'pet_reference_rubric'
    && ['speaking', 'read_aloud'].includes(prompt.item.answer_type)
}

export function originalAudioPromptPlayback(sessionId: string, result: DiagnosticPromptResult,
  options: CompanionSpeechOptions = {}): CompanionSpeechOptions {
  const prompt = result.prompt
  if (!prompt || !samePrompt(result, sessionId, prompt)) return options
  const upstream = { ...options }
  // Freeze identity before any asynchronous read, not a mutable component value.
  const expected = { child_id: prompt.child_id, prompt_id: prompt.prompt_id, item_version: prompt.item_version }
  const current = (): boolean => {
    const reference = voiceCaptureReference(sessionId, expected.child_id).question_reference
    return upstream.isCurrent?.() !== false && originalAudioCaptureRequired(sessionId, expected.child_id)
      && reference?.kind === 'diagnostic' && reference.binding.prompt_id === expected.prompt_id
      && reference.binding.item_version === expected.item_version
  }
  return { ...upstream, localOnly: true, isCurrent: current,
    beforePlayback: async signal => {
      if (!current() || signal.aborted || (upstream.beforePlayback && !await upstream.beforePlayback(signal))) return false
      const owner = await new Promise<Owner | null>(resolve => {
        let seen = owners.get(sessionId)
        let finished = false
        let activationRequested = false
        const settle = (value: Owner | null): void => {
          if (finished) return
          finished = true; listeners.delete(check); unsubscribe(); signal.removeEventListener('abort', check)
          resolve(value)
        }
        const check = (): void => {
          const value = owners.get(sessionId)
          if (signal.aborted || !current() || (seen && value !== seen)
            || (value && value.childId !== expected.child_id)) { settle(null); return }
          if (value) seen = value
          if (value && !activationRequested) { activationRequested = true; value.activate?.() }
          if (value?.ready) settle(value)
        }
        const unsubscribe = subscribeVoiceCaptureContext(check)
        listeners.add(check); signal.addEventListener('abort', check, { once: true }); check()
      })
      if (!owner || signal.aborted || !current()) return false
      // The queue admission read cannot create another question or submit an answer.
      const verified = await localApi.diagnosticPrompt(sessionId, expected.prompt_id)
      const allowed = !signal.aborted && current() && owners.get(sessionId) === owner && owner.ready
        && samePrompt(verified, sessionId, expected)
      void window.desktop?.recordRendererDiagnostic?.({ event: 'voice.original_prompt.admission',
        level: allowed ? 'info' : 'warn', details: { allowed, capture_ready: owner.ready } }).catch(() => undefined)
      return allowed
    },
  }
}
