import type { AgentTurn } from './types'

export const LEGACY_VOICE_EVENT_TIMEOUT_MS = 20_000
export class VoiceActionTimeoutError extends Error {
  constructor() { super('VOICE_ACTION_RESULT_UNCONFIRMED'); this.name = 'VoiceActionTimeoutError' }
}
export class VoiceActionUnavailableError extends Error {
  constructor(message: string) { super(message); this.name = 'VoiceActionUnavailableError' }
}

/** Legacy receivers resolve asynchronously without claiming the event. A deadline
 * only ends our wait; it never proves the operation failed or completed. */
export function requestLegacyVoiceEvent<T extends object>(name: string, detail: T, signal?: AbortSignal): Promise<boolean> {
  return new Promise((resolve, reject) => {
    let settled = false
    const cleanup = (): void => { clearTimeout(timer); signal?.removeEventListener('abort', abort) }
    const finish = (handled: boolean): void => {
      if (settled) return
      settled = true; cleanup(); resolve(handled)
    }
    const abort = (): void => {
      if (settled) return
      settled = true; cleanup(); reject(new DOMException('Voice action cancelled', 'AbortError'))
    }
    const timer = setTimeout(() => {
      if (settled) return
      settled = true; cleanup(); reject(new VoiceActionTimeoutError())
    }, LEGACY_VOICE_EVENT_TIMEOUT_MS)
    if (signal?.aborted) { abort(); return }
    signal?.addEventListener('abort', abort, { once: true })
    window.dispatchEvent(new CustomEvent(name, { cancelable: true, detail: { ...detail, resolve: finish, signal } }))
  })
}

export class VoiceRecoverySaveError extends Error {
  constructor() { super('VOICE_RECOVERY_NOT_SAVED'); this.name = 'VoiceRecoverySaveError' }
}

/** Receivers synchronously preventDefault to claim ownership, then resolve with
 * the real asynchronous outcome. Silence or elapsed time is not a receipt. */
export function requestOwnedVoiceEvent<T extends object>(name: string, detail: T, signal?: AbortSignal): Promise<boolean> {
  return new Promise((resolve, reject) => {
    let settled = false
    const abort = (): void => {
      if (settled) return
      settled = true
      signal?.removeEventListener('abort', abort)
      reject(new DOMException('Voice action cancelled', 'AbortError'))
    }
    const finish = (handled: boolean): void => {
      if (settled) return
      settled = true
      signal?.removeEventListener('abort', abort)
      resolve(handled)
    }
    if (signal?.aborted) { abort(); return }
    signal?.addEventListener('abort', abort, { once: true })
    const event = new CustomEvent(name, { cancelable: true, detail: { ...detail, resolve: finish } })
    window.dispatchEvent(event)
    if (!event.defaultPrevented) finish(false)
  })
}

export type TutorRecoveryCommand = 'retry' | 'confirm' | 'keep' | 'continue'
export function tutorRecoveryCommand(text: string): TutorRecoveryCommand | null {
  const heard = text.replace(/[\s，。！？、,.!?；;：:]/g, '')
  if (/^(重试讲解|重试本题|再试一次)$/.test(heard)) return 'retry'
  if (heard === '确认再次请求') return 'confirm'
  if (/^(保留问题|保留题干|重新问|保留问题重新问|保留题干改为新请求)$/.test(heard)) return 'keep'
  if (/^(继续当前任务|我知道了继续当前任务)$/.test(heard)) return 'continue'
  return null
}

export type LocalCourseRecoveryContext = NonNullable<NonNullable<AgentTurn['action']['local_learning_context']>['course_context']>

export function parseLocalCourseRecoveryContext(value: unknown): LocalCourseRecoveryContext | null {
  if (!value || typeof value !== 'object') return null
  const course = value as Partial<LocalCourseRecoveryContext>
  if (course.cloud_transmission_allowed !== false || course.mastery_update_allowed !== false
    || !['matched', 'no_match', 'unavailable'].includes(course.status ?? '')
    || !Array.isArray(course.evidence) || course.evidence.length > 12
    || (course.diagnostic_prompt != null && (typeof course.diagnostic_prompt !== 'string' || course.diagnostic_prompt.length > 4000))) return null
  if (course.evidence.some((item) => !item || typeof item !== 'object'
    || ['segment_id', 'resource_id', 'title', 'subject', 'grade', 'source_ref', 'excerpt', 'review_status'].some((key) => typeof item[key as keyof typeof item] !== 'string')
    || !item.segment_id || !item.resource_id || item.excerpt.length > 4000
    || !Number.isInteger(item.excerpt_start_char) || item.excerpt_start_char < 0
    || (item.page_number !== null && (!Number.isInteger(item.page_number) || item.page_number < 1))
    || item.usage !== 'source_comparison_not_verified_curriculum_alignment')) return null
  try { if (JSON.stringify(course).length > 32_000) return null } catch { return null }
  return course as LocalCourseRecoveryContext
}
