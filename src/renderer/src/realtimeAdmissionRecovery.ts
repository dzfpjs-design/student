import { acknowledgeRealtimeAdmission, pendingRealtimeAdmissions, type RealtimeAdmissionHandoff } from './realtimeAdmissionJournal'
import type { AgentRequestReceipt } from './agentRequestRecovery'
import { withReceiptRecoveryWindow, type ReceiptRecoveryWindow } from './receiptRecoveryWindow'

export type RealtimeAdmissionReceipt = {
  session_id: string; child_id: string; client_action_id: string; voice_input_id: string | null;
  status: 'not_found' | 'prepared' | 'completed' | 'cancelled';
  local_only: true; cloud_transmission_allowed: false; replay_allowed: false; mastery_update_allowed: false;
}
// Read scheduling only; this cursor never authorizes retry or execution.
const checkedAfter = new Map<string, string>()

function scopedReceipt(item: RealtimeAdmissionHandoff, receipt: RealtimeAdmissionReceipt): boolean {
  return receipt.session_id === item.sessionId && receipt.child_id === item.childId && receipt.client_action_id === item.inputId
    && ['not_found', 'prepared', 'completed', 'cancelled'].includes(receipt.status)
    && receipt.local_only === true && receipt.cloud_transmission_allowed === false
    && receipt.replay_allowed === false && receipt.mastery_update_allowed === false
    && (receipt.status === 'completed' ? typeof receipt.voice_input_id === 'string'
      && /^[A-Za-z0-9_-]{8,128}$/.test(receipt.voice_input_id) : receipt.voice_input_id === null)
    && !(item.voiceInputId !== null && receipt.status === 'completed' && item.voiceInputId !== receipt.voice_input_id)
}

export async function recoverRealtimeCancellations(sessionId: string, childId: string,
  cancel: (session: string, input: string, signal?: AbortSignal) => Promise<unknown>,
  read: (session: string, input: string, signal?: AbortSignal) => Promise<RealtimeAdmissionReceipt>,
  current: () => boolean, attempts: Map<string, number>, signal: AbortSignal
): Promise<{ confirmed: number; failures: number; retryable: boolean }> {
  const eligible = () => pendingRealtimeAdmissions(sessionId, childId)
    .filter(item => item.phase === 'cancel_requested' && (attempts.get(item.inputId) ?? 0) < 3)
  let confirmed = 0, failures = 0
  if (!current() || signal.aborted) return { confirmed, failures, retryable: false }
  for (const item of eligible().slice(0, 8)) {
    if (!current() || signal.aborted) break
    attempts.set(item.inputId, (attempts.get(item.inputId) ?? 0) + 1)
    const controller = new AbortController()
    let interrupt!: (reason: Error) => void
    const interrupted = new Promise<never>((_resolve, reject) => { interrupt = reject })
    const stop = () => { controller.abort(); interrupt(new DOMException('Cancellation recovery interrupted', 'AbortError')) }
    signal.addEventListener('abort', stop, { once: true })
    const timer = setTimeout(stop, 10_000)
    try {
      const operation = async () => {
        await cancel(sessionId, item.inputId, controller.signal)
        if (!current() || controller.signal.aborted) throw new DOMException('Recovery owner changed', 'AbortError')
        return read(sessionId, item.inputId, controller.signal)
      }
      const receipt = await Promise.race([operation(), interrupted])
      if (!current() || signal.aborted) break
      if (!scopedReceipt(item, receipt) || receipt.status !== 'cancelled') { failures++; continue }
      acknowledgeRealtimeAdmission(item)
      if (!pendingRealtimeAdmissions(sessionId, childId).some(value => value.inputId === item.inputId)) confirmed++
      else failures++
    } catch { failures++ }
    finally { clearTimeout(timer); signal.removeEventListener('abort', stop) }
  }
  return { confirmed, failures, retryable: current() && !signal.aborted && eligible().length > 0 }
}

export async function checkRealtimeAdmissions(sessionId: string, childId: string,
  read: (session: string, input: string, signal?: AbortSignal) => Promise<RealtimeAdmissionReceipt>,
  readAgent: (session: string, action: string, signal?: AbortSignal) => Promise<AgentRequestReceipt>, current: () => boolean,
  currentInputId?: string, recovery?: ReceiptRecoveryWindow): Promise<string | null> {
  if (!current()) return null
  if (!recovery) return withReceiptRecoveryWindow(current, undefined, window =>
    checkRealtimeAdmissions(sessionId, childId, read, readAgent, window.current, currentInputId, window))
  const pending = pendingRealtimeAdmissions(sessionId, childId).filter(item => item.inputId !== currentInputId)
  if (!pending.length) return null
  const ownerKey = JSON.stringify([sessionId, childId])
  const start = (pending.findIndex(item => item.inputId === checkedAfter.get(ownerKey)) + 1) % pending.length
  const ordered = [...pending.slice(start), ...pending.slice(0, start)].slice(0, 8)
  const messages: string[] = []
  for (const item of ordered) {
    if (!current()) return null
    if (checkedAfter.size >= 256) checkedAfter.delete(checkedAfter.keys().next().value!)
    checkedAfter.set(ownerKey, item.inputId)
    const receipt = await recovery.read(signal => read(sessionId, item.inputId, signal))
    if (!current()) return null
    if (!scopedReceipt(item, receipt)) {
      throw new Error('Admission receipt scope mismatch')
    }
    let message: string
    if (receipt.status === 'cancelled') {
      message = '原话的后台撤回已确认，没有重新提交。'
      acknowledgeRealtimeAdmission(item)
    } else if (item.phase === 'cancel_requested') {
      message = '本机已停止，但原话的后台撤回尚未确认；不能把本地停止当成后台取消成功。'
    } else if (receipt.status !== 'completed') {
      message = receipt.status === 'prepared' ? '原话的起始记录存在，但完整转写尚未确认保存，没有自动重发。'
        : '暂时查不到原话的保存回执，不能据此断定没有保存，没有自动重发。'
    } else {
      // Browser phase is not evidence that the Runtime never received work.
      const actionId = 'realtime-' + receipt.voice_input_id
      const agent = await recovery.read(signal => readAgent(sessionId, actionId, signal))
      if (!current()) return null
      if (agent.session_id !== sessionId || agent.client_action_id !== actionId
        || !['recorded_completed', 'recorded_failed', 'interpreted', 'received', 'unconfirmed'].includes(agent.status)
        || agent.local_only !== true || agent.cloud_transmission_allowed !== false || agent.replay_allowed !== false
        || agent.mastery_update_allowed !== false || typeof agent.response_text !== 'string'
        || !agent.response_text || agent.response_text.length > 500) throw new Error('Admission Runtime receipt mismatch')
      message = agent.response_text
      if (['recorded_completed', 'recorded_failed'].includes(agent.status)) acknowledgeRealtimeAdmission(item)
    }
    if (!messages.includes(message)) messages.push(message)
  }
  return messages.join(' ') + (pending.length > 8 ? '还有原话尚未核对，可以继续核对刚才请求。' : '')
}
