import { voiceCaptureReference, voiceCarryoverConfirmationCapture } from './voiceCaptureContext'
import type { QwenRealtimeInputContext } from './qwenRealtimeVoice'

/** A new live native utterance may start independently of an uncertain old
 * evaluation. This creates a fresh context before capture; it never edits or
 * upgrades an already captured local-only original. */
export function nativeVoiceOnsetContext(sessionId: string, childId: string): QwenRealtimeInputContext {
  const reference = voiceCaptureReference(sessionId, childId)
  if (reference.local_recovery_only === true || reference.pending_evaluation_turn_id) {
    return { sessionId, reference: { child_id: childId }, carryoverConfirmationId: null }
  }
  return { sessionId, reference, carryoverConfirmationId: voiceCarryoverConfirmationCapture(sessionId, childId) }
}
