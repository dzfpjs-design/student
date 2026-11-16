import { acknowledgeAgentRequest, pendingAgentRequests, AGENT_REQUEST_UNCONFIRMED } from './agentRequestJournal'
import { acknowledgeAgentExecution, pendingAgentExecutions, AGENT_EXECUTION_UNCONFIRMED } from './agentExecutionJournal'
import { withReceiptRecoveryWindow, type ReceiptRecoveryWindow } from './receiptRecoveryWindow'

export type AgentRequestReceipt = {
  session_id: string; client_action_id: string
  status: 'recorded_completed' | 'recorded_failed' | 'interpreted' | 'received' | 'unconfirmed'
  response_text: string; local_only: true; cloud_transmission_allowed: false
  replay_allowed: false; mastery_update_allowed: false
}

// Scheduling only, never a cached result or execution authorization. A restart
// starts scanning at the beginning of the unchanged durable identities.
const checkedAfter = new Map<string, string>()

export function restoredAgentRequestMessage(sessionId: string): string | null {
  try {
    if (pendingAgentExecutions(sessionId).length) return AGENT_EXECUTION_UNCONFIRMED
    return pendingAgentRequests(sessionId).length ? AGENT_REQUEST_UNCONFIRMED : null
  }
  catch { return '上次语音请求的本机恢复记录无法读取，不会自动重做。请家长检查本机存储。' }
}

export async function checkAgentRequests(sessionId: string,
  read: (session: string, action: string, signal?: AbortSignal) => Promise<AgentRequestReceipt>, current: () => boolean,
  recovery?: ReceiptRecoveryWindow): Promise<string | null> {
  if (!current()) return null
  if (!recovery) return withReceiptRecoveryWindow(current, undefined, window =>
    checkAgentRequests(sessionId, read, window.current, window))
  const deliveries = pendingAgentRequests(sessionId), executions = pendingAgentExecutions(sessionId)
  const pending = [...new Map([...deliveries, ...executions].map(item => [item.actionId, item])).values()]
    .sort((a, b) => a.createdAt.localeCompare(b.createdAt) || a.actionId.localeCompare(b.actionId))
  if (!pending.length) return '没有待核对的语音传输请求。需要查看学习进度时，可以问我当前任务；没有重新执行旧请求。'
  const messages: string[] = []
  // Bound one voice interaction; later checks continue the remaining queue.
  const start = (pending.findIndex(item => item.actionId === checkedAfter.get(sessionId)) + 1) % pending.length
  const ordered = [...pending.slice(start), ...pending.slice(0, start)].slice(0, 8)
  for (const item of ordered) {
    if (!current()) return null
    if (checkedAfter.size >= 256) checkedAfter.delete(checkedAfter.keys().next().value!)
    checkedAfter.set(sessionId, item.actionId)
    const receipt = await recovery.read(signal => read(sessionId, item.actionId, signal))
    if (!current()) return null
    if (receipt.session_id !== sessionId || receipt.client_action_id !== item.actionId
      || receipt.local_only !== true || receipt.cloud_transmission_allowed !== false
      || receipt.replay_allowed !== false || receipt.mastery_update_allowed !== false
      || typeof receipt.response_text !== 'string' || !receipt.response_text || receipt.response_text.length > 500
      || !['recorded_completed', 'recorded_failed', 'interpreted', 'received', 'unconfirmed'].includes(receipt.status)) {
      throw new Error('Request receipt scope mismatch')
    }
    if (!messages.includes(receipt.response_text)) messages.push(receipt.response_text)
    // Only a terminal receipt can retire the durable recovery identity.
    // Interpretation alone says nothing about whether execution finished.
    if (['recorded_completed', 'recorded_failed'].includes(receipt.status)) {
      for (const original of deliveries.filter(value => value.actionId === item.actionId)) acknowledgeAgentRequest(original)
      for (const original of executions.filter(value => value.actionId === item.actionId)) acknowledgeAgentExecution(original)
    }
  }
  return messages.join(' ') + (pending.length > 8 ? '还有原请求尚未核对，可以继续核对刚才请求。' : '')
}
