import { afterEach, describe, expect, it, vi } from 'vitest'
import { localApi, requestParentTextbookSource, resetRuntimeCache } from './api'

describe('requestParentTextbookSource', () => {
  afterEach(() => { vi.restoreAllMocks(); resetRuntimeCache(); delete (window as Window & { desktop?: unknown }).desktop })

  it('reads a bounded authenticated image from the local service', async () => {
    Object.defineProperty(window, 'desktop', { configurable: true, value: { getRuntimeConfig: vi.fn(async () => ({ status: 'ready', localApiUrl: 'http://127.0.0.1:8765', sessionToken: 'session-token' })) } })
    const fetchMock = vi.spyOn(globalThis, 'fetch')
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ parent_token: 'parent-token' }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    await localApi.parentUnlock('1234', false)
    fetchMock.mockResolvedValueOnce(new Response(new Uint8Array([137, 80, 78, 71]), { status: 200, headers: { 'Content-Type': 'image/png' } }))
    const result = await requestParentTextbookSource('child-4', 'capture-1', new AbortController().signal)
    expect(result.mediaType).toBe('image/png')
    expect(new Uint8Array(result.bytes)).toEqual(new Uint8Array([137, 80, 78, 71]))
    expect(fetchMock).toHaveBeenLastCalledWith('http://127.0.0.1:8765/v1/parent/knowledge-graph/textbook-capture/child-4/artifacts/capture-1/source', expect.objectContaining({ cache: 'no-store', redirect: 'error', headers: expect.objectContaining({ 'X-HSC-Parent': 'parent-token', 'X-HSC-Session': 'session-token' }) }))
  })

  it.each([
    ['../child', 'capture-1'],
    ['child-4', '../capture'],
  ])('rejects unsafe opaque handles', async (childId, captureRef) => {
    await expect(requestParentTextbookSource(childId, captureRef, new AbortController().signal)).rejects.toMatchObject({ code: 'PARENT_PATH_INVALID' })
  })

  it('rejects non-image and oversized responses', async () => {
    Object.defineProperty(window, 'desktop', { configurable: true, value: { getRuntimeConfig: vi.fn(async () => ({ status: 'ready', localApiUrl: 'http://127.0.0.1:8765', sessionToken: 'session-token' })) } })
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('not image', { status: 200, headers: { 'Content-Type': 'text/plain' } }))
    await expect(requestParentTextbookSource('child-4', 'capture-1', new AbortController().signal)).rejects.toThrow('原页')
    vi.mocked(fetch).mockResolvedValue(new Response(new Uint8Array(18 * 1024 * 1024 + 1), { status: 200, headers: { 'Content-Type': 'image/jpeg' } }))
    await expect(requestParentTextbookSource('child-4', 'capture-1', new AbortController().signal)).rejects.toThrow('上限')
  })
})
