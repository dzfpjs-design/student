import { ApiError, localApi } from './api'
import type { AgentTurn, VoiceInput } from './types'
import type { VoiceCaptureReference } from './voiceCaptureContext'

export const LOCAL_RECOVERY_FAILURE = '本地语音恢复没有成功，不能确认刚才的话，也没有通知家长。请直接去找身边可信任的大人；如果现在有危险，请马上向现实中的大人求助。'
export const LOCAL_RECOVERY_GUIDANCE = '原评价恢复记录需要家长核对，暂不继续学习提交。现在可以说需要什么帮助；如果有危险，请直接去找身边可信任的大人。'
export class LocalVoiceRecoveryError extends Error {
  readonly code?: string
  constructor(cause?: unknown) {
    super(LOCAL_RECOVERY_FAILURE)
    this.code = cause instanceof ApiError ? cause.code : undefined
  }
}

export function acceptsLocalRecoveryControl(turn: AgentTurn, sessionId: string, childId: string): boolean {
  const action = turn.action
  const marker = action.local_recovery_control
  const capability = action.type === 'none' ? 'conversation.respond'
    : ['find_parent', 'find_parent_urgent'].includes(action.type) ? 'safety.find_parent' : null
  return turn.session_id === sessionId && turn.child_id === childId && Boolean(turn.agent_turn_id)
    && marker?.version === 'local_voice_recovery_v1' && marker.mode === 'safety_only'
    && action.local_only === true && action.cloud_transmission_allowed === false
    && !action.pending_evaluation_control && !action.pending_evaluation_query
    && turn.after_execution === 'stop'
    && ['pending', 'completed'].includes(turn.tool_status) && capability !== null
    && turn.runtime_capability === capability
    && turn.runtime_execution_location === (action.type === 'none' ? 'server' : 'client_device_bridge')
    && (action.capability === undefined || action.capability === capability)
}

function validLocalReceipt(result: VoiceInput): boolean {
  const binding = result.capture_binding
  return result.status === 'completed' && result.cloud_used === false && Boolean(result.voice_input_id)
    && binding?.status === 'completed' && binding.local_recovery_only === true
    && !binding.pending_evaluation_turn_id && !binding.question_reference && !binding.activity_reference
    && !binding.prompt_id && !binding.item_version
}

export async function transcribeCapturedVoice(sessionId: string, childId: string | undefined, purpose: string,
  wav: string, signal: AbortSignal | undefined, key: string, reference: VoiceCaptureReference,
  isCurrent: () => boolean): Promise<VoiceInput> {
  const current = (): boolean => isCurrent() && !signal?.aborted
  if (!childId || reference.child_id !== childId || !current()) throw new LocalVoiceRecoveryError()
  const local = reference.local_recovery_only === true
  if (local && (reference.question_reference || reference.activity_reference || reference.pending_evaluation_turn_id)) {
    throw new LocalVoiceRecoveryError()
  }
  const recover = async (recoveryKey: string): Promise<VoiceInput> => {
    if (!current()) throw new LocalVoiceRecoveryError()
    try {
      const result = await localApi.transcribe(sessionId, 'agent_command', wav, signal, recoveryKey,
        { child_id: childId, local_recovery_only: true })
      if (!current() || !validLocalReceipt(result)) throw new LocalVoiceRecoveryError()
      return result
    } catch (cause) {
      if (signal?.aborted) void localApi.cancelTranscriptionAction(sessionId, recoveryKey).catch(() => undefined)
      throw new LocalVoiceRecoveryError(cause)
    }
  }
  if (local) return recover(key)
  try {
    const result = await localApi.transcribe(sessionId, reference.pending_evaluation_turn_id ? 'agent_command' : purpose,
      wav, signal, key, reference)
    if (result.capture_binding?.local_recovery_only) throw new LocalVoiceRecoveryError()
    return result
  } catch (cause) {
    if (cause instanceof ApiError && cause.code === 'VOICE_CAPTURE_PENDING_EVALUATION_CHANGED'
      && reference.pending_evaluation_turn_id && current()) return recover(`${key}:local-recovery-v1`)
    throw cause
  }
}
