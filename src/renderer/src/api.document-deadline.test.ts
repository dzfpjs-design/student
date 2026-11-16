import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { localApi, resetRuntimeCache } from './api'

const prepared = (url: string) => new Response(JSON.stringify({ session_id: 'synthetic-session',
  client_action_id: url.split('/').at(-2), operation: url.includes('/receipts/ocr/') ? 'ocr' : 'revision', status: 'prepared' }))

beforeEach(() => { resetRuntimeCache(); vi.useFakeTimers() })
afterEach(() => { resetRuntimeCache(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers() })

it.each(['ocr', 'revision'])('bounds a hung %s mutation without replaying an unknown outcome', async stage => {
  let signal!: AbortSignal
  const fetcher = vi.fn((url, init) => url.endsWith('/prepare') ? Promise.resolve(prepared(url)) : new Promise((_resolve, reject) => {
    signal = init.signal
    signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })
  }))
  vi.stubGlobal('fetch', fetcher)
  const result = stage === 'ocr' ? localApi.runDocumentMonitorOcr('synthetic-session', 'cGFnZQ==')
    : localApi.recordDocumentMonitorRevision('synthetic-session', 'synthetic-page', 'signature')
  const assertion = expect(result).rejects.toMatchObject({ code: 'DOCUMENT_OPERATION_RESULT_UNCONFIRMED' })
  await vi.advanceTimersByTimeAsync(1)
  expect(signal).toBeInstanceOf(AbortSignal)
  await vi.advanceTimersByTimeAsync((stage === 'ocr' ? 180_000 : 30_000) - 2)
  expect(signal.aborted).toBe(false)
  await vi.advanceTimersByTimeAsync(2)
  await assertion
  expect(fetcher).toHaveBeenCalledTimes(2)
  expect(vi.getTimerCount()).toBe(0)
})

it('clears the deadline after a successful saved version', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => url.endsWith('/prepare') ? prepared(url) : new Response('{"revision_id":"saved-version"}')))
  await expect(localApi.recordDocumentMonitorRevision('synthetic-session', 'synthetic-page', 'signature'))
    .resolves.toMatchObject({ revision_id: 'saved-version' })
  expect(vi.getTimerCount()).toBe(0)
})

it('does not send an image when its durable preparation was not confirmed', async () => {
  const fetcher = vi.fn().mockResolvedValue(new Response('{}'))
  vi.stubGlobal('fetch', fetcher)
  await expect(localApi.runDocumentMonitorOcr('synthetic-session', 'cGFnZQ==')).rejects.toMatchObject({ code: 'DOCUMENT_RECOVERY_NOT_PREPARED' })
  expect(fetcher).toHaveBeenCalledOnce()
  expect(fetcher.mock.calls[0][1].body).toBeUndefined()
})

it('does not send an image after privacy stop during durable preparation', async () => {
  let resolve!: (value: Response) => void
  const fetcher = vi.fn().mockReturnValue(new Promise<Response>(yes => { resolve = yes }))
  vi.stubGlobal('fetch', fetcher)
  const result = expect(localApi.runDocumentMonitorOcr('synthetic-session', 'cGFnZQ==')).rejects.toMatchObject({ code: 'DOCUMENT_OPERATION_RESULT_UNCONFIRMED' })
  await vi.advanceTimersByTimeAsync(1)
  window.dispatchEvent(new Event('hsc-privacy-pause'))
  resolve(prepared(fetcher.mock.calls[0][0]))
  await result
  expect(fetcher).toHaveBeenCalledOnce()
})

it('retains an unknown server failure rather than retrying a possibly saved image', async () => {
  const fetcher = vi.fn(async (url: string) => url.endsWith('/prepare') ? prepared(url) : new Response('{"detail":"save response lost"}', { status: 500 }))
  vi.stubGlobal('fetch', fetcher)
  await expect(localApi.runDocumentMonitorOcr('synthetic-session', 'cGFnZQ==')).rejects.toMatchObject({ code: 'DOCUMENT_OPERATION_RESULT_UNCONFIRMED' })
  expect(fetcher).toHaveBeenCalledTimes(2)
  expect(fetcher.mock.calls.filter(([url]) => url.endsWith('/ocr'))).toHaveLength(1)
})
