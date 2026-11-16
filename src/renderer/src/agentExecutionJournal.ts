import type { PendingAgentRequest } from './agentRequestJournal'

export type PendingAgentExecution = PendingAgentRequest & { childId: string; turnId: string }
const prefix = 'hsc-agent-execution-v1:'
const valid = (value: unknown): value is string => typeof value === 'string' && /^[A-Za-z0-9_-]{1,160}$/.test(value)
const key = (item: PendingAgentExecution): string => `${prefix}${item.childId}:${item.sessionId}:${item.turnId}`

export type AgentExecutionRecoveryIndex = {
  session_id: string; child_id: string; local_only: true; cloud_transmission_allowed: false; replay_allowed: false
  has_more: boolean; entries: { agent_turn_id: string; client_action_id: string; created_at: string }[]
}

// Recreate missing identity metadata only. Never replay a pending operation.
export function restoreAgentExecutionIdentities(sessionId: string, childId: string, index: AgentExecutionRecoveryIndex): number {
  if (!valid(sessionId) || !valid(childId) || !index || index.session_id !== sessionId || index.child_id !== childId
    || index.local_only !== true || index.cloud_transmission_allowed !== false || index.replay_allowed !== false
    || typeof index.has_more !== 'boolean' || !Array.isArray(index.entries) || index.entries.length > 128
    || Object.keys(index).sort().join(',') !== 'child_id,cloud_transmission_allowed,entries,has_more,local_only,replay_allowed,session_id') {
    throw Error('Execution recovery index scope mismatch')
  }
  const existing = pendingAgentExecutions(sessionId), seenTurns = new Set<string>(), seenActions = new Set<string>()
  for (const row of index.entries) {
    if (!row || !valid(row.agent_turn_id) || !valid(row.client_action_id)
      || typeof row.created_at !== 'string' || !Number.isFinite(Date.parse(row.created_at))
      || Object.keys(row).sort().join(',') !== 'agent_turn_id,client_action_id,created_at'
      || seenTurns.has(row.agent_turn_id) || seenActions.has(row.client_action_id)) throw Error('Execution recovery index entry invalid')
    seenTurns.add(row.agent_turn_id); seenActions.add(row.client_action_id)
    if (existing.some(old => (old.turnId === row.agent_turn_id || old.actionId === row.client_action_id)
      && (old.turnId !== row.agent_turn_id || old.actionId !== row.client_action_id || old.childId !== childId))) {
      throw Error('Execution recovery identity changed')
    }
  }
  const missing = index.entries.filter(row => !existing.some(old => old.turnId === row.agent_turn_id))
  if (existing.length + missing.length > 128) throw Error('Execution journal unavailable')
  for (const row of missing) saveAgentExecution({ version: 1, sessionId, actionId: row.client_action_id,
    createdAt: row.created_at }, childId, row.agent_turn_id)
  return missing.length
}

// Only delivery/execution identities. No speech, answers, action bodies or replay authority.
function readMatching(matches: (parts: string[]) => boolean): PendingAgentExecution[] {
  const result: PendingAgentExecution[] = []
  for (const name of Object.keys(localStorage)) {
    if (!name.startsWith(prefix) || !matches(name.slice(prefix.length).split(':'))) continue
    const item = JSON.parse(localStorage.getItem(name)!) as PendingAgentExecution
    if (!item || item.version !== 1 || ![item.childId, item.sessionId, item.actionId, item.turnId].every(valid)
      || key(item) !== name || typeof item.createdAt !== 'string' || !Number.isFinite(Date.parse(item.createdAt))
      || Object.keys(item).sort().join(',') !== 'actionId,childId,createdAt,sessionId,turnId,version') {
      throw Error('Invalid execution recovery identity')
    }
    result.push(item)
  }
  return result.sort((a, b) => a.createdAt.localeCompare(b.createdAt) || a.actionId.localeCompare(b.actionId))
}

export function pendingAgentExecutions(sessionId: string): PendingAgentExecution[] {
  if (!valid(sessionId)) throw Error('Invalid session identity')
  return readMatching(parts => parts[1] === sessionId)
}

export function agentExecutionSnapshot(turnId: string): PendingAgentExecution[] {
  if (!valid(turnId)) throw Error('Invalid turn identity')
  return readMatching(parts => parts[2] === turnId)
}

export function saveAgentExecution(request: PendingAgentRequest, childId: string, turnId: string): void {
  if (!valid(childId) || !valid(turnId) || !valid(request.actionId)) {
    throw Error('Execution journal unavailable')
  }
  const value: PendingAgentExecution = { ...request, childId, turnId }
  const raw = JSON.stringify(value)
  const existing = localStorage.getItem(key(value))
  if (existing !== null && existing !== raw) throw Error('Execution identity already exists')
  if (existing === raw) return
  if (pendingAgentExecutions(request.sessionId).length >= 128) throw Error('Execution journal unavailable')
  localStorage.setItem(key(value), raw)
  if (localStorage.getItem(key(value)) !== raw) throw Error('Execution journal not saved')
}

export function acknowledgeAgentExecution(item: PendingAgentExecution): void {
  if (localStorage.getItem(key(item)) === JSON.stringify(item)) localStorage.removeItem(key(item))
}

export function settleAgentExecution(snapshot: PendingAgentExecution[], turn: {
  agent_turn_id?: string; session_id?: string; child_id?: string; tool_status?: string
} | null | undefined): void {
  if (!turn || !['completed', 'failed'].includes(turn.tool_status ?? '')) return
  for (const item of snapshot) {
    if (turn.agent_turn_id === item.turnId && turn.session_id === item.sessionId && turn.child_id === item.childId) {
      try { acknowledgeAgentExecution(item) } catch { /* Preserve read-only recovery when storage is unavailable. */ }
    }
  }
}

export const AGENT_EXECUTION_UNCONFIRMED = '上次请求已被理解，但还有操作没有核对完成回执。我保留了原操作编号，不会自动重做。可以说“核对刚才请求”；请不要重复提交原答案。'
