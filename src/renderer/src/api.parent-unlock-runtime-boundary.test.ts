import { afterEach, expect, it, vi } from 'vitest'
import { localApi, resetRuntimeCache } from './api'
afterEach(() => { delete window.desktop; resetRuntimeCache(); vi.unstubAllGlobals() })
it('a parent unlock body from the old runtime cannot reinstall its grant after public restart', async () => {
  resetRuntimeCache()
  const old = { status: 'ready' as const, localApiUrl: 'http://127.0.0.1:19001', sessionToken: 'SYNTHETIC-SESSION-A' }
  const fresh = { status: 'ready' as const, localApiUrl: 'http://127.0.0.1:19002', sessionToken: 'SYNTHETIC-SESSION-B' }
  let current = old, finishOldBody!: () => void, bodyStarted!: () => void
  const started = new Promise<void>(resolve => { bodyStarted = resolve })
  const stream = new ReadableStream<Uint8Array>({ start(controller) {
    finishOldBody = () => { controller.enqueue(new TextEncoder().encode('{"parent_token":"SYNTHETIC-PARENT-A"}')); controller.close() }
  } })
  const response = new Response(stream, { status: 200, headers: { 'Content-Type': 'application/json' } })
  const nativeJson = response.json.bind(response)
  vi.spyOn(response, 'json').mockImplementation(() => { bodyStarted(); return nativeJson() })
  const desktop = { getRuntimeConfig: vi.fn(async () => current), restartLocalService: vi.fn(async () => { current = fresh; return fresh }) }
  window.desktop = desktop as unknown as NonNullable<Window['desktop']>
  const fetcher = vi.fn(async (url: string, _options?: RequestInit) => url.endsWith('/v1/parent/unlock') ? response : new Response('{}'))
  vi.stubGlobal('fetch', fetcher)
  await localApi.bootstrap()
  const unlock = localApi.parentUnlock('2468', false)
  const outcome = unlock.then(() => ({ kind: 'resolved' as const }), error => ({ kind: 'rejected' as const, error }))
  await started
  await localApi.restartRuntime()
  finishOldBody()
  const result = await outcome
  await localApi.parentToday()
  expect.soft(result.kind).toBe('rejected')
  if (result.kind === 'rejected') expect.soft(result.error).toMatchObject({ code: 'LOCAL_SERVICE_CHANGED' })
  expect(fetcher.mock.lastCall?.[0]).toBe(fresh.localApiUrl + '/v1/parent/today')
  const headers = new Headers(fetcher.mock.lastCall?.[1]?.headers)
  expect(headers.get('X-HSC-Session')).toBe(fresh.sessionToken)
  expect.soft(headers.get('X-HSC-Parent')).not.toBe('SYNTHETIC-PARENT-A')
  expect(desktop.restartLocalService).toHaveBeenCalledTimes(1)
  expect(fetcher).toHaveBeenCalledTimes(3)
})
