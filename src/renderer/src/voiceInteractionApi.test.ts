import { afterEach, expect, it, vi } from 'vitest'
import { localApi, resetRuntimeCache } from './api'

afterEach(() => { resetRuntimeCache(); vi.unstubAllGlobals() })

it('posts the exact voice interaction contract with session auth and cancellation', async () => {
  resetRuntimeCache()
  vi.stubGlobal('desktop', { getRuntimeConfig: async () => ({ status: 'ready', localApiUrl: 'http://127.0.0.1:8765', sessionToken: 'test-token' }) })
  const response = { route: 'clarify', action: 'none', response_text: '我们先聊一下。', source: 'cloud' }
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => response })
  vi.stubGlobal('fetch', fetch)
  const body = { transcript: '我还没想好名字', client_action_id: 'voice-action-1', context: { phase: 'confirm_name' as const, pending_name: '小月' } }
  const signal = new AbortController().signal
  expect(await localApi.voiceInteraction('session/a', body, signal)).toEqual(response)
  expect(fetch).toHaveBeenCalledExactlyOnceWith('http://127.0.0.1:8765/v1/sessions/session%2Fa/voice-interaction', {
    method: 'POST', body: JSON.stringify(body), signal,
    headers: { 'Content-Type': 'application/json', 'X-HSC-Session': 'test-token' },
  })
})
