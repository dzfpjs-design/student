import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ApiError, localApi, resetRuntimeCache } from './api'

const busy = () => new Response(JSON.stringify({ code: 'CALIBRATION_WRITER_BUSY',
  message: '正在同步学习记录，请稍后重试。', retryable: true, recovery_action: 'retry_same_request' }),
  { status: 503, headers: { 'Retry-After': '1' } })

beforeEach(() => { resetRuntimeCache(); vi.useFakeTimers() })
afterEach(() => { delete window.desktop; localStorage.clear(); resetRuntimeCache(); vi.useRealTimers(); vi.unstubAllGlobals() })

it('retries a voice turn once with the exact original operation, transcript and headers', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(busy()).mockResolvedValueOnce(new Response('{"agent_turn_id":"saved"}'))
  vi.stubGlobal('fetch', fetcher)
  const result = localApi.agentTurn('session-1', '帮我看看刚才做的题', undefined, 'capture-1')
  const assertion = expect(result).resolves.toEqual({ agent_turn_id: 'saved' })
  await vi.advanceTimersByTimeAsync(1500)
  await assertion
  expect(fetcher).toHaveBeenCalledTimes(2)
  expect(fetcher.mock.calls[1]).toEqual(fetcher.mock.calls[0])
  expect(JSON.parse(fetcher.mock.calls[1][1].body)).toMatchObject({ transcript: '帮我看看刚才做的题', voice_input_id: 'capture-1', client_action_id: expect.any(String) })
})

it('does not loop when the contention persists', async () => {
  const fetcher = vi.fn().mockImplementation(async () => busy())
  vi.stubGlobal('fetch', fetcher)
  const assertion = expect(localApi.agentTurn('session-1', '继续学习')).rejects.toMatchObject({ code: 'CALIBRATION_WRITER_BUSY', status: 503 })
  await vi.advanceTimersByTimeAsync(5000)
  await assertion
  expect(fetcher).toHaveBeenCalledTimes(2)
})

it.each([403, 409, 500])('never retries a different HTTP failure (%s)', async status => {
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ code: 'CALIBRATION_WRITER_BUSY', retryable: true, recovery_action: 'retry_same_request' }), { status }))
  vi.stubGlobal('fetch', fetcher)
  await expect(localApi.agentTurn('session-1', '继续学习')).rejects.toBeInstanceOf(ApiError)
  expect(fetcher).toHaveBeenCalledTimes(1)
})

it('does not retry unknown submission outcomes or unapproved routes', async () => {
  const fetcher = vi.fn().mockRejectedValueOnce(new TypeError('connection lost')).mockResolvedValueOnce(busy())
  vi.stubGlobal('fetch', fetcher)
  await expect(localApi.agentTurn('session-1', '继续学习')).rejects.toMatchObject({ code: 'AGENT_REQUEST_UNCONFIRMED' })
  await expect(localApi.generatePlan('session-1')).rejects.toMatchObject({ code: 'CALIBRATION_WRITER_BUSY' })
  expect(fetcher).toHaveBeenCalledTimes(2)
})

it.each([{ retryable: false, recovery_action: 'retry_same_request' },
  { retryable: true, recovery_action: 'check_original_receipt' }])('requires explicit safe recovery metadata (%j)', async metadata => {
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ code: 'CALIBRATION_WRITER_BUSY', ...metadata }), { status: 503 }))
  vi.stubGlobal('fetch', fetcher)
  await expect(localApi.agentTurn('session-1', '继续学习')).rejects.toBeInstanceOf(ApiError)
  expect(fetcher).toHaveBeenCalledTimes(1)
})

it('does not resend an old voice operation after the local runtime was reset', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(busy())
  vi.stubGlobal('fetch', fetcher)
  const assertion = expect(localApi.agentTurn('session-1', '继续学习')).rejects.toBeInstanceOf(ApiError)
  await vi.advanceTimersByTimeAsync(100)
  resetRuntimeCache()
  await vi.advanceTimersByTimeAsync(2000)
  await assertion
  expect(fetcher).toHaveBeenCalledTimes(1)
})

it('does not resend an old voice operation after the child stops all listening', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(busy()).mockResolvedValueOnce(new Response('{"agent_turn_id":"new-session-turn"}'))
  vi.stubGlobal('fetch', fetcher)
  const assertion = expect(localApi.agentTurn('session-1', '继续学习')).rejects.toMatchObject({ name: 'AbortError' })
  await vi.advanceTimersByTimeAsync(100)
  window.dispatchEvent(new CustomEvent('hsc-privacy-pause'))
  await vi.advanceTimersByTimeAsync(2000)
  await assertion
  expect(fetcher).toHaveBeenCalledTimes(1)
  await expect(localApi.agentTurn('session-2', '查看学习安排')).resolves.toEqual({ agent_turn_id: 'new-session-turn' })
  expect(fetcher).toHaveBeenCalledTimes(2)
})
