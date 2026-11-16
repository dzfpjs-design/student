import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { localApi, requestDiagnosticVisual, requestTutorVisual, resetRuntimeCache } from './api'
const path = '/v1/sessions/study/diagnostic-visual?prompt_id=p&item_version=v'
beforeEach(() => {
  resetRuntimeCache()
  window.desktop = { getRuntimeConfig: async () => ({ status: 'ready', localApiUrl: 'http://127.0.0.1:19085', sessionToken: 'synthetic-session' }) } as never
})
afterEach(() => { delete window.desktop; resetRuntimeCache(); vi.unstubAllGlobals() })
it('keeps the exact saved-tutor route local and separate from diagnostic and parent authority', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ parent_token: 'synthetic-parent' })))
    .mockResolvedValueOnce(new Response('{}', { headers: { 'Content-Type': 'application/json' } }))
  vi.stubGlobal('fetch', fetcher)
  await localApi.parentUnlock('2468', false)
  const signal = new AbortController().signal, tutorPath = '/v1/sessions/study/tutor-visual?turn_id=original'
  expect(await requestTutorVisual(tutorPath, signal)).toEqual({})
  expect(fetcher.mock.calls[1]).toEqual(['http://127.0.0.1:19085'+tutorPath, { method: 'GET', signal, cache: 'no-store', redirect: 'error',
    headers: { 'X-HSC-Session': 'synthetic-session' } }])
  for (const invalid of [path, tutorPath.replace('study', '..'), tutorPath.replace('study', '%2e%2e'), '/v1/parent/children/child']) {
    await expect(requestTutorVisual(invalid, signal)).rejects.toThrow()
  }
  expect(fetcher).toHaveBeenCalledTimes(2)
})
it('never borrows an unlocked parent token for child figure reads', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ parent_token: 'synthetic-parent' })))
    .mockResolvedValueOnce(new Response('{}', { headers: { 'Content-Type': 'application/json' } }))
  vi.stubGlobal('fetch', fetcher)
  await localApi.parentUnlock('2468', false)
  const signal = new AbortController().signal
  expect(await requestDiagnosticVisual(path, signal)).toEqual({})
  expect(fetcher.mock.calls[1]).toEqual(['http://127.0.0.1:19085'+path, { method: 'GET', signal, cache: 'no-store', redirect: 'error',
    headers: { 'X-HSC-Session': 'synthetic-session' } }])
})
it.each(['https://example.com', 'http://127.0.0.1/path', 'http://user:secret@127.0.0.1'])('refuses nonlocal or malformed runtime origin %s', async origin => {
  window.desktop = { getRuntimeConfig: async () => ({ status: 'ready', localApiUrl: origin, sessionToken: 'test' }) } as never
  const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher)
  await expect(requestDiagnosticVisual(path, new AbortController().signal)).rejects.toThrow('本机')
  expect(fetcher).not.toHaveBeenCalled()
})
it('refuses other routes and traversal', async () => {
  const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher)
  for (const invalid of [path.replace('study', '..'), path.replace('study', '%2e%2e'), '/v1/parent/children/a', 'https://example.com']) {
    await expect(requestDiagnosticVisual(invalid, new AbortController().signal)).rejects.toThrow()
  }
  expect(fetcher).not.toHaveBeenCalled()
})
it.each([206, 302, 409])('does not accept partial, redirected or stale results %s', async status => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('pixels', { status, headers: { 'Content-Type': 'image/png' } })))
  await expect(requestDiagnosticVisual(path.replace('?', '/image?'), new AbortController().signal)).rejects.toThrow()
})
it.each([false, true])('bounds streamed payloads including JSON (image=%s)', async image => {
  const cancel = vi.fn(), stream = new ReadableStream({ start(c) { c.enqueue(new Uint8Array((image ? 2*1024*1024 : 64*1024)+1)) }, cancel })
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(stream, { headers: { 'Content-Type': image ? 'image/png' : 'application/json' } })))
  await expect(requestDiagnosticVisual(image ? path.replace('?', '/image?') : path, new AbortController().signal)).rejects.toThrow('上限')
  expect(cancel).toHaveBeenCalledOnce(); expect(stream.locked).toBe(false)
})
