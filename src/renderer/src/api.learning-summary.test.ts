import { afterEach, describe, expect, it, vi } from 'vitest'
import { localApi, resetRuntimeCache } from './api'

describe('daily closure transport recovery', () => {
  afterEach(() => { delete window.desktop; resetRuntimeCache(); vi.unstubAllGlobals() })
  const payload = { learnedText: '分数比较', difficultText: '通分', feeling: '', nextAction: '再试一题' }
  it('recovers authoritative completed state after a lost response and keeps a stable retry key', async () => {
    window.desktop = { getRuntimeConfig: async () => ({ localApiUrl: 'http://127.0.0.1:8766', sessionToken: 'synthetic', status: 'ready' }) } as never
    const saved = { session: { session_id: 'session-a', status: 'completed' } }
    const fetcher = vi.fn().mockRejectedValueOnce(new Error('response lost')).mockResolvedValueOnce({ ok: true, json: async () => saved })
    vi.stubGlobal('fetch', fetcher)
    expect(await localApi.completeReflection('session-a', payload)).toEqual(saved)
    expect(JSON.parse(fetcher.mock.calls[0][1].body).client_action_id).toBe('reflection-close:session-a')
    expect(fetcher.mock.calls[1][0]).toBe('http://127.0.0.1:8766/v1/sessions/session-a')
  })
  it('does not pretend to have closed an active session after a failed write', async () => {
    window.desktop = { getRuntimeConfig: async () => ({ localApiUrl: 'http://127.0.0.1:8766', sessionToken: 'synthetic', status: 'ready' }) } as never
    vi.stubGlobal('fetch', vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({ ok: true, json: async () => ({ session: { session_id: 'session-a', status: 'active' } }) }))
    await expect(localApi.completeReflection('session-a', payload)).rejects.toThrow()
  })
})
