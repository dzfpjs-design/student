import { afterEach, expect, it, vi } from 'vitest'
import { createPaperVariantAuthorizationApi, clearPaperVariantAuthorizationLocalState, paperVariantAuthorizationKey } from './paperVariantAuthorizationApi'

export const paperView = (overrides = {}) => ({ child_id: 'child-a', mistake_id: 'mistake-a', revision: 0,
  status: 'authorization_required', reason: null, message: '请核对这道原题。', question_preview: '一根绳子长24米，平均分成3段，每段长多少米？',
  question_version: 'question-v1', source_version: 'source-v1', authorization_id: null,
  license_options: ['CC0-1.0', 'original_author', 'explicit_cloud_processing_permission'],
  rights_review_authority: 'parent_declaration_not_legal_or_network_verification', local_only: true, cloud_transmission_allowed: false, ...overrides })
const command = { child_id: 'child-a', mistake_id: 'mistake-a', operation: 'grant' as const, body: { expected_revision: 0, client_action_id: 'fixed-key',
  question_version: 'question-v1', source_version: 'source-v1', license_basis: { license_id: 'original_author' as const, reference: '原创题目声明', evidence: '我是这道隔离合成题目的原创作者，仅声明这道题文的处理用途。' } } }
afterEach(() => { localStorage.clear(); vi.restoreAllMocks() })
it('reads the parent-only local route and submits only the exact body, without an actor', async () => {
  const request = vi.fn().mockResolvedValueOnce(paperView()).mockResolvedValueOnce({ local_only: true, cloud_transmission_allowed: false,
    receipt: { event_id: 'event1', revision: 1, command: 'grant' }, current: paperView({ status: 'authorized', revision: 1, authorization_id: 'event1' }),
    generation_job: { job_id: 'job1', status: 'pending', requested_count: 3, error_code: null } })
  const api = createPaperVariantAuthorizationApi(request)
  await api.read('child-a', 'mistake-a'); const result = await api.perform(command)
  expect(request.mock.calls[0]).toEqual(['/v1/parent/children/child-a/paper-variant-authorizations/mistake-a', { cache: 'no-store' }])
  expect(JSON.parse(request.mock.calls[1][1].body)).toEqual(command.body)
  expect(request.mock.calls[1][0]).toMatch(/mistake-a\/grant$/)
  expect(result.generation_job?.job_id).toBe('job1')
})
it.each([{ child_id: 'other-child' }, { mistake_id: 'other-mistake' }, { license_options: [{ id: 'invented' }] }, { cloud_transmission_allowed: true },
  { status: 'authorized', authorization_id: null }])('rejects a mismatched or unsupported DTO %j', async patch => {
  const api = createPaperVariantAuthorizationApi(vi.fn().mockResolvedValue(paperView(patch)))
  await expect(api.read('child-a', 'mistake-a')).rejects.toThrow()
})
it('preserves current revoked status when an old grant receipt and stopped job are returned', async () => {
  const api = createPaperVariantAuthorizationApi(vi.fn().mockResolvedValue({ local_only: true, cloud_transmission_allowed: false,
    receipt: { event_id: 'event1', revision: 1, command: 'grant' }, current: paperView({ revision: 2, status: 'revoked' }),
    generation_job: { job_id: 'job1', status: 'blocked', requested_count: 3, error_code: 'PAPER_VARIANT_AUTHORIZATION_REVOKED' } }))
  const result = await api.perform(command)
  expect(result.current.status).toBe('revoked'); expect(result.generation_job?.status).toBe('blocked')
})
it('rejects injected actor and malformed recovery commands before a network call', async () => {
  const request = vi.fn(), api = createPaperVariantAuthorizationApi(request)
  await expect(api.perform({ ...command, body: { ...command.body, actor_id: 'parent_ui' } } as never)).rejects.toThrow()
  await expect(api.perform({ ...command, operation: 'revoke' } as never)).rejects.toThrow()
  expect(request).not.toHaveBeenCalled()
})
it('clears an exact child prefix or all permission journals without touching others', () => {
  const a = paperVariantAuthorizationKey('child-a', 'mistake-a'), b = paperVariantAuthorizationKey('child-ab', 'mistake-b')
  localStorage.setItem(a, 'pending'); localStorage.setItem(b, 'pending'); localStorage.setItem('hsc-writing-authorization:child-a:item', 'keep')
  clearPaperVariantAuthorizationLocalState('child-a')
  expect(localStorage.getItem(a)).toBeNull(); expect(localStorage.getItem(b)).toBe('pending')
  clearPaperVariantAuthorizationLocalState()
  expect(localStorage.getItem(b)).toBeNull(); expect(localStorage.getItem('hsc-writing-authorization:child-a:item')).toBe('keep')
})
