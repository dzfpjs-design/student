import type { VoiceInteractionContext, VoiceInteractionRecovery } from './types'

export const isVoiceInteractionRetry = (text: string): boolean => /^(重试|再试一次)[。！!]?$/u.test(text.trim())
export const voiceInteractionContextKey = (value: VoiceInteractionContext): string =>
  JSON.stringify([value.phase, value.step ?? null, value.pending_name ?? null, value.wake_word ?? null])

export function validatedVoiceInteractionRecovery(value: VoiceInteractionRecovery, sessionId: string, childId: string,
  context: VoiceInteractionContext): VoiceInteractionRecovery {
  if (!value || value.session_id !== sessionId || value.child_id !== childId
    || !['none', 'retryable', 'pending', 'outcome_unknown', 'completed'].includes(value.status)) {
    throw new Error('VOICE_INTERACTION_RECOVERY_SCOPE_CHANGED')
  }
  if (value.status === 'none') {
    if (value.request || value.response || value.original_action_key) throw new Error('INVALID_VOICE_INTERACTION_RECOVERY')
    return value
  }
  const request = value.request
  if (!request || !value.original_action_key || typeof request.transcript !== 'string' || !request.transcript.trim()
    || typeof request.client_action_id !== 'string' || !request.client_action_id
    || (request.voice_input_id !== undefined && (typeof request.voice_input_id !== 'string' || !request.voice_input_id))) {
    throw new Error('INVALID_VOICE_INTERACTION_RECOVERY')
  }
  const original = request.context
  // The backend proves this hydration from the same child's latest saved
  // cloud candidate and the unchanged desktop profile. No local name guess.
  const hydratedName = context.phase === 'naming' && !context.pending_name
    && original.phase === 'confirm_name' && typeof original.pending_name === 'string'
    && original.pending_name.trim().length >= 2 && original.pending_name.length <= 12
    && original.step === context.step && original.wake_word === context.wake_word
  if (voiceInteractionContextKey(original) !== voiceInteractionContextKey(context) && !hydratedName) {
    throw new Error('VOICE_INTERACTION_RECOVERY_CONTEXT_CHANGED')
  }
  if (value.status === 'completed' && (!value.response || value.response.source !== 'cloud')) {
    throw new Error('INVALID_VOICE_INTERACTION_RECOVERY')
  }
  return value
}

export function voiceInteractionRecoveryMessage(status: VoiceInteractionRecovery['status'] | 'unreadable'): string {
  if (status === 'pending' || status === 'outcome_unknown') return '刚才的话还在核对处理结果，我不会重复提交。请稍后说“再试一次”核对原记录。'
  if (status === 'unreadable') return '刚才的话还没有确认完成，恢复记录暂时无法确认。我不会重复提交，请稍后说“再试一次”。'
  if (status === 'none') return '没有找到当前设置中需要重试的话，请重新说出你想做的事。'
  return '刚才的话已保存，但设置还没有确认完成。说“再试一次”，我会接着处理刚才的话。'
}
