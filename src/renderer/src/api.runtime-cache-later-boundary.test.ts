import { afterEach, expect, it, vi } from 'vitest'
import { localApi, resetRuntimeCache } from './api'
afterEach(() => { delete window.desktop; resetRuntimeCache(); vi.unstubAllGlobals() })
it('public restart must retire old identity reacquired at a later manual unlock boundary', async () => {
  resetRuntimeCache()
  let current = { status: 'ready' as const, localApiUrl: 'http://127.0.0.1:19001', sessionToken: 'SYNTHETIC-A' }
  let finish!: () => void
  const pending = new Promise<void>(resolve => { finish = resolve })
  const desktop = { getRuntimeConfig: vi.fn(async () => current), restartLocalService: vi.fn(() => pending) }
  window.desktop = desktop as unknown as NonNullable<Window['desktop']>
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    const token = new Headers(options?.headers).get('X-HSC-Session')
    if (!url.startsWith(current.localApiUrl + '/') || token !== current.sessionToken)
      return new Response('{"code":"LOCAL_SESSION_REQUIRED","message":"SYNTHETIC stale identity"}', { status: 401 })
    return new Response('{}')
  })
  vi.stubGlobal('fetch', fetcher)
  await localApi.bootstrap()
  const restart = localApi.restartRuntime()
  // Actual ParentPetAudioSettings.tsx:108 invokes this exported boundary.
  // The main handler is still awaiting saved-provider load; its old child is ready.
  resetRuntimeCache()
  await localApi.bootstrap()
  current = { ...current, localApiUrl: 'http://127.0.0.1:19002', sessionToken: 'SYNTHETIC-B' }
  finish(); await restart
  await expect(localApi.bootstrap()).resolves.toEqual({})
  expect(fetcher.mock.lastCall?.[0]).toBe(current.localApiUrl + '/v1/bootstrap')
  expect(desktop.restartLocalService).toHaveBeenCalledTimes(1)
})
