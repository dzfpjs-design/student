import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { localApi, requestParentVisualImage, requestParentVisualLocal, resetRuntimeCache } from './api'

const path = `/v1/parent/children/child-1/visual-materials/page-1/regions/figure/image?revision_id=r-1&region_version=${'a'.repeat(64)}&sha256=${'b'.repeat(64)}`
beforeEach(() => {
  resetRuntimeCache()
  window.desktop = { getRuntimeConfig: async () => ({ status: 'ready', localApiUrl: 'http://127.0.0.1:19085', sessionToken: 'synthetic-session' }) } as never
})
afterEach(() => { delete window.desktop; resetRuntimeCache(); vi.unstubAllGlobals() })
it('reads bounded local PNG and its display token with real configured authorization headers', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ parent_token: 'synthetic-parent' })))
    .mockResolvedValueOnce(new Response(new Uint8Array([1, 2]), { headers: { 'Content-Type': 'image/png', 'X-HSC-Visual-Display-Token': 'local-test-token' } }))
  vi.stubGlobal('fetch', fetcher)
  await localApi.parentUnlock('2468', false)
  const signal = new AbortController().signal
  const image = await requestParentVisualImage(path, signal)
  expect(image.display_token).toBe('local-test-token')
  expect(new Uint8Array(image.bytes)).toEqual(new Uint8Array([1, 2]))
  expect(fetcher.mock.calls[1]).toEqual([`http://127.0.0.1:19085${path}`, { method: 'GET', signal, cache: 'no-store', redirect: 'error',
    headers: { 'X-HSC-Session': 'synthetic-session', 'X-HSC-Parent': 'synthetic-parent' } }])
})
it.each(['https://example.com', 'http://127.0.0.1:19085/other', 'http://user:secret@127.0.0.1:19085'])('never sends image or JSON credentials to invalid origin %s', async origin => {
  window.desktop = { getRuntimeConfig: async () => ({ status: 'ready', localApiUrl: origin, sessionToken: 'synthetic-session' }) } as never
  const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher)
  await expect(requestParentVisualImage(path, new AbortController().signal)).rejects.toThrow('本机')
  await expect(requestParentVisualLocal(path.replace('/image?', '?'))).rejects.toThrow('本机')
  expect(fetcher).not.toHaveBeenCalled()
})
it('rejects traversal, nonvisual routes and redirects rather than requesting substitute material', async () => {
  const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher)
  for (const invalid of [path.replace('child-1', '..'), path.replace('child-1', '%2e%2e'), path.replace('page-1', '%2fadmin'),
    path.replace('page-1', 'page%5cother'), '/v1/parent/privacy', 'https://example.com']) {
    await expect(requestParentVisualImage(invalid, new AbortController().signal)).rejects.toThrow()
  }
  expect(fetcher).not.toHaveBeenCalled()
})
it.each([{ status: 206, token: 'a', type: 'image/png' }, { status: 200, token: '', type: 'image/png' }, { status: 200, token: 'a', type: 'text/html' }])('does not treat a partial/wrong/unbound body as the original %#', async spec => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('bytes', { status: spec.status, headers: { 'Content-Type': spec.type, 'X-HSC-Visual-Display-Token': spec.token } })))
  await expect(requestParentVisualImage(path, new AbortController().signal)).rejects.toThrow('未加载替代图片')
})
it('cancels oversized streaming PNG before allocating an unbounded buffer', async () => {
  const cancel = vi.fn(), stream = new ReadableStream({ start(c) { c.enqueue(new Uint8Array(2 * 1024 * 1024 + 1)) }, cancel })
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(stream, { headers: { 'Content-Type': 'image/png', 'X-HSC-Visual-Display-Token': 'local-token' } })))
  await expect(requestParentVisualImage(path, new AbortController().signal)).rejects.toThrow('上限')
  expect(cancel).toHaveBeenCalledOnce(); expect(stream.locked).toBe(false)
})
it('accepts the full scoped display token contract but rejects tokens above its bound', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(new Response(new Uint8Array([1, 2]), { headers: { 'Content-Type': 'image/png', 'X-HSC-Visual-Display-Token': 'a'.repeat(1093) } }))
    .mockResolvedValueOnce(new Response(new Uint8Array([1, 2]), { headers: { 'Content-Type': 'image/png', 'X-HSC-Visual-Display-Token': 'a'.repeat(6001) } }))
  vi.stubGlobal('fetch', fetcher)
  expect((await requestParentVisualImage(path, new AbortController().signal)).display_token).toHaveLength(1093)
  await expect(requestParentVisualImage(path, new AbortController().signal)).rejects.toThrow('凭证无法核对')
})
