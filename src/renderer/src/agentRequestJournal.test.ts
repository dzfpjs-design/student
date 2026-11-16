import { afterEach, expect, it, vi } from 'vitest'
import { acknowledgeAgentRequest, isAgentRequestCheck, pendingAgentRequests, saveAgentRequest } from './agentRequestJournal'

afterEach(() => { localStorage.clear(); vi.restoreAllMocks() })

it('keeps delivery IDs across reload without speech or other child records', async () => {
  const first = saveAgentRequest('session-first', 'operation-1')
  saveAgentRequest('session-other', 'operation-2')
  vi.resetModules()
  const restored = await import('./agentRequestJournal')
  expect(restored.pendingAgentRequests('session-first')).toEqual([first])
  expect(Object.keys(first).sort()).toEqual(['actionId', 'createdAt', 'sessionId', 'version'])
  acknowledgeAgentRequest(first)
  expect(pendingAgentRequests('session-first')).toEqual([])
  expect(pendingAgentRequests('session-other')).toHaveLength(1)
})

it('does not delete a changed journal while an old acknowledgement is in flight', () => {
  const first = saveAgentRequest('session-first', 'operation-1')
  const key = Object.keys(localStorage)[0]
  localStorage.setItem(key, JSON.stringify({ ...first, createdAt: '2026-01-01T00:00:00Z' }))
  acknowledgeAgentRequest(first)
  expect(pendingAgentRequests('session-first')).toHaveLength(1)
})

it('fails closed on malformed identities, corrupt data and exhausted storage', () => {
  expect(() => saveAgentRequest('../other', 'operation-1')).toThrow()
  localStorage.setItem('hsc-agent-request-v1:session-first:operation-1', '{broken')
  expect(() => pendingAgentRequests('session-first')).toThrow()
  expect(() => saveAgentRequest('session-first', 'operation-2')).toThrow()
  localStorage.clear()
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('full') })
  expect(() => saveAgentRequest('session-first', 'operation-1')).toThrow()
})

it('recognizes explicit local receipt queries, not answers, denials or quoted commands', () => {
  for (const text of ['核对刚才请求', '请查看上次的请求状态', '帮我查询一下原语音请求结果']) expect(isAgentRequestCheck(text)).toBe(true)
  for (const text of ['不要核对刚才请求', '老师让我说核对刚才请求', '12', '开始任务']) expect(isAgentRequestCheck(text)).toBe(false)
})
