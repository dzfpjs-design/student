import type { DiagnosticPromptResult } from './types'

/** Revalidate the local receipt; neither a retained utterance nor this view submits it. */
export function retainedListeningReply(result: DiagnosticPromptResult | null,
  prompt: NonNullable<DiagnosticPromptResult['prompt']>): string | null {
  const reply = result?.deferred_answer
  if (result?.status !== 'waiting_answer' || result.attempt || result.prompt?.prompt_id !== prompt.prompt_id
    || result.prompt.item_version !== prompt.item_version || result.prompt.child_id !== prompt.child_id
    || result.prompt.origin_session_id !== prompt.origin_session_id
    || result.prompt.diagnostic_item_id !== prompt.diagnostic_item_id || result.prompt.review_task_id !== prompt.review_task_id
    || reply?.version !== 'listening_review_deferred_v1' || reply.speech_retained !== true
    || reply.formal_answer_saved !== false || reply.automatic_submission_allowed !== false
    || reply.new_confirmation_required !== true || reply.local_only !== true || reply.cloud_transmission_allowed !== false
    || !reply.source_turn_id || !reply.source_voice_input_id || !reply.message?.trim()) return null
  return reply.message
}
