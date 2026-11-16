type DeliveryPolicy = { deliver: boolean; localOnly: boolean }

export function evaluationFeedbackPolicy(value: unknown): DeliveryPolicy {
  const result = value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : {}
  const localOnly = result.local_only === true || result.cloud_transmission_allowed === false
  if (!('feedback_delivery_allowed' in result)) {
    return { deliver: result.objective_update_applied !== false, localOnly }
  }
  const oral = result.oral_feedback as Record<string, unknown> | undefined
  const audio = oral?.audio_work_ref as Record<string, unknown> | undefined
  const formativeAudio = oral?.status === 'formative_audio_feedback' && audio !== null && typeof audio === 'object'
    && typeof audio.work_id === 'string' && audio.work_id.length > 0 && audio.work_id.length <= 200
    && Number.isSafeInteger(audio.generation) && Number(audio.generation) >= 0
    && ['source_hash', 'result_hash'].every(key => typeof audio[key] === 'string' && /^[a-f0-9]{64}$/.test(audio[key] as string))
  // Formative feedback may be spoken without certifying mastery, but only the
  // server's current original-answer projection can grant that exception.
  const deliver = result.feedback_delivery_allowed === true && result.feedback_status === 'ready'
    && result.objective_update_applied === false && result.local_only === true
    && result.cloud_transmission_allowed === false && oral?.version === 'oral_teaching_v2'
    && (['content_self_check', 'reviewed_audio_feedback'].includes(String(oral.status)) || formativeAudio)
    && oral.local_only === true && oral.cloud_transmission_allowed === false
    && oral.mastery_claim_allowed === false
    && ['prompt_id', 'item_version', 'feedback_version', 'source_version'].every(
      key => typeof oral[key] === 'string' && (oral[key] as string).length > 0)
  return { deliver, localOnly }
}
