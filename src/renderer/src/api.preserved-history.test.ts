import { afterEach, expect, it, vi } from 'vitest'
import { localApi, resetRuntimeCache } from './api'

afterEach(() => { delete window.desktop; resetRuntimeCache(); vi.unstubAllGlobals() })

it('uses the current parent unlock and runtime token for original-answer history', async () => {
  resetRuntimeCache()
  window.desktop = { getRuntimeConfig: async () => ({ localApiUrl: 'http://127.0.0.1:19085', sessionToken: 'synthetic-session', status: 'ready' }) } as never
  const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ parent_token: 'synthetic-parent' })))
    .mockResolvedValueOnce(new Response(JSON.stringify({ items: [] })))
  vi.stubGlobal('fetch', fetcher)
  await localApi.parentUnlock('2468', false)
  await expect(localApi.preservedMistakeResponses('child-1', 'case-2')).resolves.toEqual({ items: [] })
  expect(fetcher.mock.calls[1][0]).toBe('http://127.0.0.1:19085/v1/parent/children/child-1/mistakes/case-2/preserved-responses')
  expect(fetcher.mock.calls[1][1]).toMatchObject({ cache: 'no-store', headers: {
    'X-HSC-Session': 'synthetic-session', 'X-HSC-Parent': 'synthetic-parent'
  } })
})
