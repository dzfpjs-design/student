export type PendingAgentRequest = { version: 1; sessionId: string; actionId: string; createdAt: string }
const prefix = 'hsc-agent-request-v1:'
const segment = (value: string): boolean => typeof value === 'string' && /^[A-Za-z0-9_-]{1,160}$/.test(value)
const key = (session: string, action: string): string => `${prefix}${session}:${action}`

// Delivery identities only. Original speech, images and model replies stay out
// of this journal; a recovery read never authorizes executing an old command.
export function pendingAgentRequests(sessionId: string): PendingAgentRequest[] {
  if (!segment(sessionId)) throw new Error('Invalid session identity')
  const values: PendingAgentRequest[] = []
  for (const name of Object.keys(localStorage)) {
    if (!name.startsWith(`${prefix}${sessionId}:`)) continue
    const item = JSON.parse(localStorage.getItem(name)!) as PendingAgentRequest
    if (!item || item.version !== 1 || item.sessionId !== sessionId || !segment(item.actionId)
      || name !== key(sessionId, item.actionId) || typeof item.createdAt !== 'string'
      || !Number.isFinite(Date.parse(item.createdAt))
      || Object.keys(item).sort().join(',') !== 'actionId,createdAt,sessionId,version') throw new Error('Invalid request journal')
    values.push(item)
  }
  return values.sort((a, b) => a.createdAt.localeCompare(b.createdAt) || a.actionId.localeCompare(b.actionId))
}

export function saveAgentRequest(sessionId: string, actionId: string): PendingAgentRequest {
  if (!segment(actionId) || pendingAgentRequests(sessionId).length >= 128) throw new Error('Request journal unavailable')
  const value: PendingAgentRequest = { version: 1, sessionId, actionId, createdAt: new Date().toISOString() }
  if (localStorage.getItem(key(sessionId, actionId)) !== null) throw new Error('Request identity already exists')
  const serialized = JSON.stringify(value)
  localStorage.setItem(key(sessionId, actionId), serialized)
  if (localStorage.getItem(key(sessionId, actionId)) !== serialized) throw new Error('Request journal not saved')
  return value
}

export function acknowledgeAgentRequest(value: PendingAgentRequest): void {
  const name = key(value.sessionId, value.actionId)
  if (localStorage.getItem(name) === JSON.stringify(value)) localStorage.removeItem(name)
}

export const AGENT_REQUEST_UNCONFIRMED = '刚才的请求没有收到完整回执，不能确认是否处理完成。我保留了原请求编号，不会自动重做。可以说“核对刚才请求”；请不要重复提交刚才的答案。'
export const AGENT_REQUEST_SAVE_FAILED = '原请求的恢复记录没有保存成功，这句话尚未提交。请保持页面，请家长检查本机存储。'
export const AGENT_HANDOFF_UNCONFIRMED = '原话已有交接记录，但结果或来源尚需核对。我保留了原请求编号，不会自动重做。可以说“核对刚才请求”，不用重复提交答案。'

export function isNonRepeatableAgentHandoff(status: number | undefined, code: string | undefined): boolean {
  return status === 409 && (code === 'AGENT_HANDOFF_UNCONFIRMED' || code === 'AGENT_HANDOFF_SOURCE_CONFLICT')
}

export function isAgentRequestCheck(text: string): boolean {
  return /^(?:请|帮我)?(?:核对|查询|查看)(?:一下)?(?:刚才|上次|原)(?:的)?(?:语音)?请求(?:状态|结果)?[。！？!?]?$/u.test(text.trim())
}
