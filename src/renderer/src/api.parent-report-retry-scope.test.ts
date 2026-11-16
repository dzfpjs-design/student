import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { localApi, resetRuntimeCache } from './api'

beforeEach(() => {
  resetRuntimeCache()
  window.desktop = { getRuntimeConfig: vi.fn(async () => ({
    status: 'ready', localApiUrl: 'http://127.0.0.1:19001', sessionToken: 'SYNTHETIC-SESSION'
  })) } as unknown as NonNullable<Window['desktop']>
})
afterEach(() => { delete window.desktop; resetRuntimeCache(); vi.unstubAllGlobals() })

it('rejects a misrouted retry response before callers can cache it under the requested child', async () => {
  const foreign = { report_id: 'SYNTHETIC-FOREIGN-REPORT', child_id: 'child-junior-3', session_id: 'SYNTHETIC-FOREIGN-SESSION' }
  const fetcher = vi.fn(async () => new Response(JSON.stringify(foreign), { status: 200 }))
  vi.stubGlobal('fetch', fetcher)
  const outcome = await localApi.retryChildReport('child-primary-4').then(
    value => ({ value, error: null }), error => ({ value: null, error })
  )
  expect(outcome.value).toBeNull()
  expect(outcome.error).toMatchObject({ code: 'REPORT_CHILD_SCOPE_CHANGED' })
  expect(outcome.error.message).not.toContain(foreign.child_id)
  expect(outcome.error.message).not.toContain(foreign.report_id)
  expect(outcome.error.message).not.toContain(foreign.session_id)
  expect(fetcher).toHaveBeenCalledTimes(1)
})

it('preserves the matching child report from the real request response', async () => {
  const report = { report_id: 'SYNTHETIC-REPORT', child_id: 'child-primary-4', session_id: 'SYNTHETIC-SESSION' }
  const fetcher = vi.fn(async (_url: string, _init?: RequestInit) => new Response(JSON.stringify(report), { status: 200 }))
  vi.stubGlobal('fetch', fetcher)
  await expect(localApi.retryChildReport('child-primary-4')).resolves.toEqual(report)
  expect(fetcher.mock.calls[0]?.[0]).toBe('http://127.0.0.1:19001/v1/parent/children/child-primary-4/reports/retry')
})
