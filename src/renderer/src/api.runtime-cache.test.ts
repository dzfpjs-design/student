import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { localApi, resetRuntimeCache } from './api'

const deferred = <T,>() => {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const runtime = (port: number, token: string) => ({ status: 'ready' as const,
  localApiUrl: `http://127.0.0.1:${port}`, sessionToken: token })

beforeEach(() => resetRuntimeCache())
afterEach(() => { delete window.desktop; localStorage.clear(); resetRuntimeCache(); vi.unstubAllGlobals() })

function fixture(next = runtime(19002, 'SYNTHETIC-B')) {
  let current = runtime(19001, 'SYNTHETIC-A')
  const pending = deferred<typeof current>()
  const desktop = { getRuntimeConfig: vi.fn(async () => current),
    restartLocalService: vi.fn(() => pending.promise) }
  window.desktop = desktop as unknown as NonNullable<Window['desktop']>
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    const token = new Headers(options?.headers).get('X-HSC-Session')
    if (!url.startsWith(current.localApiUrl + '/') || token !== current.sessionToken)
      return new Response(JSON.stringify({ code: 'LOCAL_SESSION_REQUIRED', message: 'SYNTHETIC stale identity' }), { status: 401 })
    return new Response(url.endsWith('/v1/parent/unlock') ? '{"parent_token":"SYNTHETIC-PARENT-B"}' : '{}')
  })
  vi.stubGlobal('fetch', fetcher)
  return { desktop, fetcher, pending, finish: () => { current = next; pending.resolve(next) },
    setNext: () => { current = next }, current: () => current, next }
}

describe('public renderer runtime restart cache boundary', () => {
  it('serial public restart refreshes the URL and token before the next request', async () => {
    const f = fixture(); await localApi.bootstrap()
    const restart = localApi.restartRuntime(); f.finish(); await restart
    await localApi.bootstrap()
    expect(f.fetcher.mock.lastCall?.[0]).toBe(f.next.localApiUrl + '/v1/bootstrap')
    expect(f.desktop.restartLocalService).toHaveBeenCalledTimes(1)
  })

  it('direct desktop IPC bypass remains distinct from the renderer public restart contract', async () => {
    const f = fixture(); await localApi.bootstrap()
    const restart = window.desktop!.restartLocalService(); f.finish(); await restart
    await expect(localApi.bootstrap()).rejects.toMatchObject({ status: 401, code: 'LOCAL_SESSION_REQUIRED' })
    expect(f.fetcher.mock.lastCall?.[0]).toBe('http://127.0.0.1:19001/v1/bootstrap')
    expect(f.desktop.restartLocalService).toHaveBeenCalledTimes(1)
  })

  it.each([runtime(19002, 'SYNTHETIC-B'), runtime(19001, 'SYNTHETIC-B')])(
    'clears identity recached while real asynchronous restart admission is pending (%o)', async next => {
      const f = fixture(next); await localApi.bootstrap()
      const restart = localApi.restartRuntime()
      // Actual runtime:restart awaits provider loadSnapshot before manager restart;
      // getRuntimeConfig may still legally return the old ready child here.
      await localApi.bootstrap()
      f.finish(); await restart
      await expect(localApi.bootstrap()).resolves.toEqual({})
      expect(f.fetcher.mock.lastCall?.[0]).toBe(next.localApiUrl + '/v1/bootstrap')
      expect(new Headers(f.fetcher.mock.lastCall?.[1]?.headers).get('X-HSC-Session')).toBe(next.sessionToken)
      expect(f.fetcher).toHaveBeenCalledTimes(3)
    })

  it('preserves the original IPC failure while invalidating identity cached during its unknown outcome', async () => {
    const f = fixture(); await localApi.bootstrap()
    const restart = localApi.restartRuntime(); const error = new Error('SYNTHETIC refresh result lost')
    const rejection = expect(restart).rejects.toBe(error)
    await localApi.bootstrap(); f.setNext(); f.pending.reject(error); await rejection
    await expect(localApi.bootstrap()).resolves.toEqual({})
    expect(f.desktop.restartLocalService).toHaveBeenCalledTimes(1)
    expect(f.fetcher).toHaveBeenCalledTimes(3)
  })

  it('rejects a late original runtime lookup without forwarding after restart settles', async () => {
    const f = fixture(); await localApi.bootstrap()
    const restart = localApi.restartRuntime(), lookup = deferred<ReturnType<typeof runtime>>()
    f.desktop.getRuntimeConfig.mockImplementationOnce(() => lookup.promise)
    const request = localApi.bootstrap()
    const rejection = expect(request).rejects.toMatchObject({ code: 'LOCAL_SERVICE_CHANGED' })
    f.finish(); await restart; lookup.resolve(runtime(19001, 'SYNTHETIC-A')); await rejection
    expect(f.fetcher).toHaveBeenCalledTimes(1)
    await expect(localApi.bootstrap()).resolves.toEqual({})
    expect(f.fetcher).toHaveBeenCalledTimes(2)
  })

  it('an older restart completion does not clear a later explicit boundary and fresh parent authorization', async () => {
    const f = fixture(); await localApi.bootstrap()
    const restart = localApi.restartRuntime()
    f.setNext(); resetRuntimeCache()
    await localApi.parentUnlock('2468', false)
    f.pending.resolve(f.next); await restart
    await localApi.parentToday()
    expect(new Headers(f.fetcher.mock.lastCall?.[1]?.headers).get('X-HSC-Parent')).toBe('SYNTHETIC-PARENT-B')
    expect(f.desktop.restartLocalService).toHaveBeenCalledTimes(1)
  })
})


describe('actual ready identity across later renderer boundaries', () => {
  it('preserves the newest public restart grant when an older restart settles or rejects later', async () => {
    for (const olderFails of [false, true]) {
      resetRuntimeCache()
      const f = fixture(), older = deferred<ReturnType<typeof runtime>>()
      f.desktop.restartLocalService.mockImplementationOnce(() => older.promise)
      const first = localApi.restartRuntime()
      const error = new Error('SYNTHETIC older restart result lost')
      const settled = olderFails ? expect(first).rejects.toBe(error) : first
      const second = localApi.restartRuntime()
      f.finish(); await second
      resetRuntimeCache(); await localApi.parentUnlock('2468', false)
      if (olderFails) older.reject(error)
      else older.resolve(runtime(19003, 'SYNTHETIC-OLDER-RECEIPT'))
      await settled; await localApi.parentToday()
      expect(new Headers(f.fetcher.mock.lastCall?.[1]?.headers).get('X-HSC-Parent')).toBe('SYNTHETIC-PARENT-B')
      expect(f.desktop.restartLocalService).toHaveBeenCalledTimes(2)
    }
  })

  it('a delayed old lookup cannot overwrite a preserved later ready identity and parent grant', async () => {
    const f = fixture(); await localApi.bootstrap()
    const restart = localApi.restartRuntime(), lookup = deferred<ReturnType<typeof runtime>>()
    resetRuntimeCache()
    f.desktop.getRuntimeConfig.mockImplementationOnce(() => lookup.promise)
    const request = localApi.bootstrap()
    const rejected = expect(request).rejects.toMatchObject({ code: 'LOCAL_SERVICE_CHANGED' })
    f.setNext(); await localApi.parentUnlock('2468', false)
    f.finish(); await restart
    lookup.resolve(runtime(19001, 'SYNTHETIC-A')); await rejected
    expect(f.fetcher).toHaveBeenCalledTimes(2)
    await localApi.parentToday()
    expect(new Headers(f.fetcher.mock.lastCall?.[1]?.headers).get('X-HSC-Parent')).toBe('SYNTHETIC-PARENT-B')
    expect(f.fetcher).toHaveBeenCalledTimes(3)
  })
})

describe('Qwen rotation recovery authority', () => {
  it('requires parent unlock and a currently limited runtime before invoking the privileged bridge', async () => {
    const f = fixture()
    const bridge = vi.fn().mockResolvedValue({ status: 'completed' })
    Object.assign(f.desktop, { rotationRecoveryContinue: bridge })
    await expect(localApi.continueQwenRotationRecovery()).rejects.toThrow('ROTATION_PARENT_AUTH_REQUIRED')
    await localApi.parentUnlock('2468', false)
    await expect(localApi.continueQwenRotationRecovery()).rejects.toThrow('ROTATION_AUTH_CHANGED')
    expect(bridge).not.toHaveBeenCalled()
    f.desktop.getRuntimeConfig.mockResolvedValue({ ...f.current(), rotationAuthOnly: true } as ReturnType<typeof runtime>)
    await localApi.continueQwenRotationRecovery()
    expect(bridge).toHaveBeenCalledExactlyOnceWith('SYNTHETIC-PARENT-B')
    await expect(localApi.continueQwenRotationRecovery()).rejects.toThrow('ROTATION_PARENT_AUTH_REQUIRED')
  })

  it('does not let a late continuation receipt clear a newer parent session', async () => {
    const f = fixture(), pending = deferred<{ status: 'completed' }>()
    const bridge = vi.fn(() => pending.promise)
    Object.assign(f.desktop, { rotationRecoveryContinue: bridge })
    f.desktop.getRuntimeConfig.mockImplementation(async () => ({ ...f.current(), rotationAuthOnly: true }))
    await localApi.parentUnlock('2468', false)
    const result = localApi.continueQwenRotationRecovery()
    const rejected = expect(result).rejects.toThrow('ROTATION_AUTH_CHANGED')
    await vi.waitFor(() => expect(bridge).toHaveBeenCalledOnce())
    f.setNext(); resetRuntimeCache(); await localApi.parentUnlock('2468', false)
    pending.resolve({ status: 'completed' }); await rejected
    await localApi.parentToday()
    expect(new Headers(f.fetcher.mock.lastCall?.[1]?.headers).get('X-HSC-Parent')).toBe('SYNTHETIC-PARENT-B')
  })
})
