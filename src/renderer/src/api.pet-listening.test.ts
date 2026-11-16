import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { localApi, requestParentPetAudio, requestParentPetLocal, requestPetListeningAudio, requestParentRubricAudio, resetRuntimeCache } from './api'
const sha = 'a'.repeat(64)
const rubricPath = `/v1/parent/children/child-1/learning-rubrics/review-2/audio?media_id=media-3&audio_sha256=${sha}`
beforeEach(() => {
  resetRuntimeCache()
  window.desktop = { getRuntimeConfig: async () => ({ localApiUrl: 'http://127.0.0.1:19085', sessionToken: 'synthetic-session', status: 'ready' }) } as never
})
afterEach(() => { delete window.desktop; resetRuntimeCache(); vi.unstubAllGlobals() })
it('reads rubric original audio only with local runtime and parent tokens, never caller-supplied transport headers', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ parent_token: 'synthetic-parent' })))
    .mockResolvedValueOnce(new Response(new Uint8Array([1, 2]), { headers: { 'Content-Type': 'audio/wav' } }))
  vi.stubGlobal('fetch', fetcher)
  await localApi.parentUnlock('2468', false)
  const signal = new AbortController().signal
  expect(new Uint8Array(await requestParentRubricAudio(rubricPath, { signal, headers: { Range: 'bytes=0-1', 'X-HSC-Parent': 'wrong' } }))).toEqual(new Uint8Array([1, 2]))
  expect(fetcher.mock.calls[1]).toEqual([`http://127.0.0.1:19085${rubricPath}`, {
    method: 'GET', signal, cache: 'no-store', redirect: 'error', headers: { 'X-HSC-Session': 'synthetic-session', 'X-HSC-Parent': 'synthetic-parent' },
  }])
  for (const path of ['https://example.com', rubricPath + '&extra=1', rubricPath.replace('child-1', '..'), rubricPath.replace('/audio?', '/review?')]) {
    await expect(requestParentRubricAudio(path)).rejects.toMatchObject({ code: 'PARENT_PATH_INVALID' })
  }
  await expect(requestParentRubricAudio(rubricPath, { method: 'POST' })).rejects.toMatchObject({ code: 'PARENT_PATH_INVALID' })
  expect(fetcher).toHaveBeenCalledTimes(2)
})
it('never forwards rubric parent tokens to a remote runtime or buffers oversized original audio', async () => {
  window.desktop = { getRuntimeConfig: async () => ({ localApiUrl: 'https://example.com', sessionToken: 'synthetic-session', status: 'ready' }) } as never
  const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher)
  await expect(requestParentRubricAudio(rubricPath)).rejects.toThrow('本机')
  expect(fetcher).not.toHaveBeenCalled()
  resetRuntimeCache()
  window.desktop = { getRuntimeConfig: async () => ({ localApiUrl: 'http://127.0.0.1:19085', sessionToken: 'synthetic-session', status: 'ready' }) } as never
  const cancel = vi.fn()
  const stream = new ReadableStream({ start(controller) { controller.enqueue(new Uint8Array(24 * 1024 * 1024 + 1)) }, cancel })
  fetcher.mockResolvedValueOnce(new Response(stream, { headers: { 'Content-Type': 'audio/wav' } }))
  await expect(requestParentRubricAudio(rubricPath)).rejects.toThrow('上限')
  expect(cancel).toHaveBeenCalledOnce()
})
it('uses only the authenticated local binary endpoint, forbids redirects and caching', async () => {
  const fetcher = vi.fn().mockResolvedValue(new Response(new Uint8Array([1, 2, 3]), { headers: { 'Content-Type': 'audio/wav' } }))
  vi.stubGlobal('fetch', fetcher)
  expect(new Uint8Array(await requestPetListeningAudio('s/1', 'm/1', sha, new AbortController().signal))).toEqual(new Uint8Array([1, 2, 3]))
  expect(fetcher).toHaveBeenCalledWith(`http://127.0.0.1:19085/v1/sessions/s%2F1/pet-media/m%2F1/audio?audio_sha256=${sha}`,
    expect.objectContaining({ redirect: 'error', cache: 'no-store', headers: { 'X-HSC-Session': 'synthetic-session' } }))
})
it('does not send the session secret to a nonlocal configured host', async () => {
  window.desktop = { getRuntimeConfig: async () => ({ localApiUrl: 'https://example.com', sessionToken: 'synthetic-session', status: 'ready' }) } as never
  const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher)
  await expect(requestPetListeningAudio('s', 'm', sha, new AbortController().signal)).rejects.toThrow('本机')
  expect(fetcher).not.toHaveBeenCalled()
})
it('cancels oversized streams rather than collecting unbounded bytes', async () => {
  const cancel = vi.fn(), stream = new ReadableStream({ start(controller) { controller.enqueue(new Uint8Array(24 * 1024 * 1024 + 1)) }, cancel })
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(stream, { headers: { 'Content-Type': 'audio/wav' } })))
  await expect(requestPetListeningAudio('s', 'm', sha, new AbortController().signal)).rejects.toThrow('上限')
  expect(cancel).toHaveBeenCalledOnce()
  expect(stream.locked).toBe(false)
})
it('rejects substituted content types and preserves abort failures', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(new Response('not audio', { headers: { 'Content-Type': 'text/html' } }))
    .mockRejectedValueOnce(new DOMException('cancelled', 'AbortError'))
  vi.stubGlobal('fetch', fetcher)
  await expect(requestPetListeningAudio('s', 'm', sha, new AbortController().signal)).rejects.toThrow('替代内容')
  await expect(requestPetListeningAudio('s', 'm', sha, new AbortController().signal)).rejects.toMatchObject({ name: 'AbortError' })
})

it('parent audition and material requests carry real unlock tokens without relaxing endpoint restrictions', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ parent_token: 'synthetic-parent' })))
    .mockResolvedValueOnce(new Response(JSON.stringify({ media: {} })))
    .mockResolvedValueOnce(new Response(new Uint8Array([1, 2]), { headers: { 'Content-Type': 'audio/wav' } }))
  vi.stubGlobal('fetch', fetcher)
  await localApi.parentUnlock('2468', false)
  await requestParentPetLocal('/v1/sessions/s-1/pet-media/prompt/p-1?item_version=v1%3Aabc')
  expect(fetcher.mock.calls[1][1]).toMatchObject({ cache: 'no-store', redirect: 'error', headers: {
    'X-HSC-Parent': 'synthetic-parent', 'X-HSC-Session': 'synthetic-session',
  } })
  await requestParentPetAudio('s-1', 'm-1', sha, new AbortController().signal)
  expect(fetcher.mock.calls[2][0]).toContain('/m-1/review-audio?')
  expect(fetcher.mock.calls[2][1].headers['X-HSC-Parent']).toBe('synthetic-parent')
  for (const path of ['/v1/sessions/s-1/pet-media/../privacy', 'https://example.com',
    '/v1/sessions/s-1/pet-media/m-1/assessment', '/v1/sessions/s-1/pet-media/prompt/p-1?else=1']) {
    await expect(requestParentPetLocal(path)).rejects.toMatchObject({ code: 'PARENT_PATH_INVALID' })
  }
  expect(fetcher).toHaveBeenCalledTimes(3)
})
