import type { QwenRealtimeInputCapture } from './qwenRealtimeVoice'
import { voiceCaptureReference, voiceCarryoverConfirmationCapture } from './voiceCaptureContext'

export type RealtimeInputSourceStatus = 'current' | 'missing' | 'unbound' | 'owner_changed' | 'source_changed'
const referenceKey = (value: unknown): string => JSON.stringify(value, (_key, item: unknown) =>
  item !== null && typeof item === 'object' && !Array.isArray(item)
    ? Object.fromEntries(Object.entries(item).sort(([a], [b]) => a.localeCompare(b))) : item)

export function sameRealtimeInputCapture(left: QwenRealtimeInputCapture, right: QwenRealtimeInputCapture): boolean {
  return referenceKey(left) === referenceKey(right)
}

export function realtimeInputSourceStatus(
  capture: QwenRealtimeInputCapture | undefined, sessionId: string, childId: string,
): RealtimeInputSourceStatus {
  if (!capture) return 'missing'
  if (capture.sessionId !== sessionId || capture.reference.child_id !== childId) return 'owner_changed'
  if (capture.source !== 'accepted_onset') return 'unbound'
  if (referenceKey(capture.reference) !== referenceKey(voiceCaptureReference(sessionId, childId))
    || capture.carryoverConfirmationId !== voiceCarryoverConfirmationCapture(sessionId, childId)) return 'source_changed'
  return 'current'
}

export function realtimeInputSourceMessage(status: Exclude<RealtimeInputSourceStatus, 'current'>): string {
  return status === 'source_changed'
    ? '刚才这句话对应的题目或活动状态已经变化，我不会把它当成另一题的答案。请再说清楚题号和你的作答。'
    : '刚才这句话没有取得说话时的来源，我不会把它当成当前题目的答案。请再说一次题号和你的作答。'
}
