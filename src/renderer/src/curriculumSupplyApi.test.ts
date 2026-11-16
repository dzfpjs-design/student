import { afterEach, describe, expect, it, vi } from 'vitest'
import { clearCurriculumSupplyLocalState, createCurriculumSupplyApi, curriculumLocalKey, loadCurriculumLocalState } from './curriculumSupplyApi'
import type { CurriculumDraftState, CurriculumParentRequest, CurriculumPending } from './curriculumSupplyTypes'

const local = { local_only: true, cloud_transmission_allowed: false }
const pending: CurriculumPending = { child_id: 'child-a', kind: 'review', candidate_id: 'candidate-a', body: { action_id: 'same-action', expected_version: 2, decision: 'approve', note: '已核对' } }
const saved: CurriculumDraftState = { sourceId: '', semester: '', standardKey: '', excerptId: '', seedId: '', explanation: '', note: '已核对', focusedId: 'candidate-a', pending }

describe('curriculumSupplyApi host contract', () => {
  afterEach(() => localStorage.clear())
  it('uses the injected parent request and preserves exact command identity on retry', async () => {
    const request = vi.fn<CurriculumParentRequest>().mockResolvedValue({ ...local, child_id: 'child-a', candidate_id: 'candidate-a' })
    const api = createCurriculumSupplyApi(request)
    await api.perform('child-a', pending)
    await api.perform('child-a', pending)
    expect(request.mock.calls[0]).toEqual(request.mock.calls[1])
    expect(request.mock.calls[0]).toEqual(['/api/parent/children/child-a/curriculum-supply/candidate-a/review', { method: 'POST', body: JSON.stringify(pending.body) }])
  })
  it('rejects another child or a non-local response and never transmits another child pending command', async () => {
    const request = vi.fn<CurriculumParentRequest>().mockResolvedValue({ ...local, child_id: 'child-b' })
    const api = createCurriculumSupplyApi(request)
    await expect(api.audit('child-a')).rejects.toThrow('当前孩子不一致')
    request.mockResolvedValue({ ...local, child_id: 'child-a', cloud_transmission_allowed: true })
    await expect(api.options('child-a')).rejects.toThrow('当前孩子不一致')
    request.mockClear()
    await expect(api.perform('child-b', pending)).rejects.toThrow('另一名孩子')
    expect(request).not.toHaveBeenCalled()
  })
  it('checks requested source and candidate identities', async () => {
    const request = vi.fn<CurriculumParentRequest>().mockResolvedValue({ ...local, child_id: 'child-a', source_id: 'wrong', candidate_id: 'wrong' })
    const api = createCurriculumSupplyApi(request)
    await expect(api.sourceOptions('child-a', 'book-a')).rejects.toThrow('教材响应已过期')
    await expect(api.get('child-a', 'candidate-a')).rejects.toThrow('题目响应已过期')
  })
  it('restores only the owned child pending state and removes it for privacy cleanup', () => {
    localStorage.setItem(curriculumLocalKey('child-a'), JSON.stringify(saved))
    expect(loadCurriculumLocalState('child-a')).toEqual(saved)
    expect(loadCurriculumLocalState('child-b')).toEqual({})
    localStorage.setItem(curriculumLocalKey('child-b'), JSON.stringify(saved))
    expect(() => loadCurriculumLocalState('child-b')).toThrow('pending')
    clearCurriculumSupplyLocalState('child-a')
    expect(loadCurriculumLocalState('child-a')).toEqual({})
    expect(localStorage.getItem(curriculumLocalKey('child-b'))).not.toBeNull()
  })
})
