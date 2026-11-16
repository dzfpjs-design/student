import type { TutorTurn } from './types'

export type TutorCompletion = {
  version: 1
  sessionId: string
  childId: string
  turnId: string
  actionId: string
  response: string
  draftText: string
  tutor?: TutorTurn
  localOnly: boolean
  acknowledged: boolean
  presentationStarted: boolean
  receipt: {
    capability: string
    execution_location: 'local_model' | 'client_device_bridge'
    action_type: 'ask_tutor' | 'more_tutor_hint' | 'record_tutor_feedback'
    status: 'completed'
    completed_at: string
    output: Record<string, unknown>
  }
}
const prefix = (sessionId: string): string => `hsc-tutor-completion-v1:${encodeURIComponent(sessionId)}:`
const key = (item: TutorCompletion): string => `${prefix(item.sessionId)}${encodeURIComponent(item.turnId)}`

// Local result evidence never enters model planning. Keep the original receipt
// and privacy policy together so retries cannot regenerate either of them.
export function saveTutorCompletion(item: TutorCompletion): void {
  localStorage.setItem(key(item), JSON.stringify(item))
}

export function readTutorCompletions(sessionId: string, childId: string): TutorCompletion[] {
  const items: TutorCompletion[] = []
  for (let index = 0; index < localStorage.length; index++) {
    const name = localStorage.key(index)!
    if (!name.startsWith(prefix(sessionId))) continue
    const item = JSON.parse(localStorage.getItem(name)!) as TutorCompletion
    const receipt = item?.receipt
    if (item?.version !== 1 || item.sessionId !== sessionId || item.childId !== childId || key(item) !== name
      || [item.turnId, item.actionId].some(id => typeof id !== 'string' || !id || id.length > 160)
      || typeof item.response !== 'string' || typeof item.draftText !== 'string' || typeof item.localOnly !== 'boolean'
      || typeof item.acknowledged !== 'boolean' || typeof item.presentationStarted !== 'boolean'
      || receipt?.status !== 'completed' || !Number.isFinite(Date.parse(receipt.completed_at))
      || !['ask_tutor', 'more_tutor_hint', 'record_tutor_feedback'].includes(receipt.action_type)
      || receipt.capability !== ({ ask_tutor: 'learning.explain', more_tutor_hint: 'tutor.more_hint', record_tutor_feedback: 'tutor.record_feedback' })[receipt.action_type]
      || receipt.execution_location !== (receipt.action_type === 'ask_tutor' ? 'local_model' : 'client_device_bridge')
      || !receipt.output || typeof receipt.output !== 'object'
      || (receipt.action_type !== 'record_tutor_feedback' && (!item.tutor || item.tutor.session_id !== sessionId || item.tutor.child_id !== childId
        || !item.tutor.turn_id || item.tutor.answer !== item.response || receipt.output.tutor_turn_id !== item.tutor.turn_id))) throw new Error('TUTOR_COMPLETION_INVALID')
    items.push(item)
  }
  return items.sort((a, b) => a.receipt.completed_at.localeCompare(b.receipt.completed_at))
}

export function clearTutorCompletion(item: TutorCompletion): void {
  localStorage.removeItem(key(item))
}

export function tutorCompletionMessage(item: TutorCompletion): string {
  const result = item.tutor ? '讲解结果已经取得' : '你的反馈已经保存'
  return item.acknowledged
    ? `${result}，回执也已确认，恢复记录还没清理完成。重试只清理记录，不会再次请求模型或重复保存反馈。`
    : `${result}，但回执还没确认。重试只核对原回执，不会再次请求模型或重复保存反馈。`
}

export async function waitForTutorReceipt<T>(request: Promise<T>): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    return await Promise.race([request, new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => reject(new Error('TUTOR_RECEIPT_UNCONFIRMED')), 10_000)
    })])
  } finally { clearTimeout(timer) }
}
