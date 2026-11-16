import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import * as api from './api'
import { SpecializedReviewQueue } from './SpecializedReviewQueue'
import { checkOriginalReview, emptyReview, readSpecializedReviews, saveSpecializedReviews,
  specializedReviewCleared, specializedReviewKey, type ReviewBody } from './specializedReviewRecovery'
import type { SpecializedSupplyCandidate } from './types'
import type { ReviewContext } from './specializedReviewRecovery'

const candidate: SpecializedSupplyCandidate = { candidate_id: 'typed29', child_id: 'child-a', job_id: 'job', track: 'competition_math',
  subject: '数学', domain_id: 'counting', seed_item_id: 'seed', stage: 'transfer', prompt: '三种颜色两种形状共有多少种搭配？',
  expected_answer: { exact_answers: ['6'], local_rule: 'local_exact_v1', answer_format: 'reviewed_exact_text_v1' },
  explanation: '三种颜色分别与两种形状搭配。', difficulty: 2, review_workflow: 'model',
  validation: { passed: true, errors: [], ruleset: 'specialized_closed_exact_v1' },
  status: 'needs_review', diagnostic_item_id: null, reviewed_at: null, review_note: null, created_at: '2026-09-11' }
const context: ReviewContext = { schema: 'specialized_stage_review_v1', child_id: 'child-a', candidate_id: 'typed29',
  binding_version: 'version29', source_root_item_id: 'seed', knowledge_node_id: 'counting', source_prompt: '两件衣服三条裤子共有多少种搭配？',
  candidate_prompt: candidate.prompt, candidate_explanation: candidate.explanation, status: 'needs_review',
  candidate_expected_answer: candidate.expected_answer,
  allowed_stages: ['near_transfer', 'far_transfer'], qualification_scope: 'explicit_human_source_stage_review_not_child_mastery',
  local_only: true, cloud_transmission_allowed: false }
function receipt(body: ReviewBody) {
  return { ...candidate, status: body.decision === 'approve' ? 'promoted' : 'rejected', diagnostic_item_id: body.decision === 'approve' ? 'published29' : null,
    review_action_id: body.client_action_id, review_note: body.note, validation: { ...candidate.validation,
      ...(body.expected_binding_version ? { review_binding_version: body.expected_binding_version } : {}),
      ...(body.stage_review ? { stage_review: body.stage_review } : {}) } }
}
function setup(onSaved = vi.fn()) { return render(<SpecializedReviewQueue childId="child-a" candidates={[candidate]} onSaved={onSaved} />) }
async function draft() {
  fireEvent.change(await screen.findByLabelText('专项候选题1审核备注'), { target: { value: '已独立核对完整参考答案和题意。' } })
  fireEvent.click(screen.getByRole('button', { name: '核对当前原题' }))
  await screen.findByText(context.source_prompt)
  await waitFor(() => expect(screen.getByRole('button', { name: '批准' })).toBeEnabled())
}
afterEach(() => { cleanup(); vi.restoreAllMocks(); localStorage.clear() })

it('persists draft and exact source binding through remount without granting a transfer stage', async () => {
  const request = vi.spyOn(api, 'requestParentLocal').mockResolvedValue(context)
  const view = setup(); await draft()
  const saved = readSpecializedReviews('child-a').value.entries[0]
  expect(saved.binding).toBe('version29'); expect(saved.stage).toBe('')
  view.unmount(); setup()
  expect(await screen.findByLabelText('专项候选题1审核备注')).toHaveValue(saved.note)
  expect(screen.getByRole('button', { name: '批准' })).toBeDisabled()
  expect(request).toHaveBeenCalledTimes(1)
})

it('recovers a lost committed response after remount even when candidate left the pending list using GET only', async () => {
  let committed: ReturnType<typeof receipt> | null = null
  const request = vi.spyOn(api, 'requestParentLocal').mockImplementation(async (path, init) => {
    if (init?.method === 'POST') {
      const body = JSON.parse(String(init.body)) as ReviewBody
      expect(readSpecializedReviews('child-a').value.entries[0].pending).toEqual(body)
      committed = receipt(body); throw new TypeError('response body lost after commit')
    }
    return path.includes('stage-review-context') ? context : { items: [committed] }
  })
  const view = setup(); await draft()
  fireEvent.click(screen.getByRole('button', { name: '批准' }))
  await screen.findByText(/审核回执未确认/)
  const pending = readSpecializedReviews('child-a').value.entries[0].pending!
  view.unmount()
  const done = vi.fn()
  render(<SpecializedReviewQueue childId="child-a" candidates={[]} onSaved={done} />)
  fireEvent.click(await screen.findByRole('button', { name: '核对原审核' }))
  await waitFor(() => expect(done).toHaveBeenCalledTimes(1))
  expect(committed!.review_action_id).toBe(pending.client_action_id)
  expect(request.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
  expect(readSpecializedReviews('child-a').value.entries).toEqual([])
  expect(screen.getByText(/发布记录不代表当前来源仍有效/)).toBeInTheDocument()
})

it('sends an explicit retry with byte-identical original body only after read-only checks', async () => {
  const posts: string[] = []
  const request = vi.spyOn(api, 'requestParentLocal').mockImplementation(async (path, init) => {
    if (init?.method === 'POST') { posts.push(String(init.body)); if (posts.length === 1) throw new TypeError('offline'); return receipt(JSON.parse(posts[0])) }
    return path.includes('stage-review-context') ? context : { items: [candidate] }
  })
  setup(); await draft(); fireEvent.click(screen.getByRole('button', { name: '批准' }))
  await screen.findByText(/审核回执未确认/)
  fireEvent.click(screen.getByRole('button', { name: '核对原审核' }))
  fireEvent.click(await screen.findByRole('button', { name: '重试原审核' }))
  await screen.findByText(/原审核已保存/)
  expect(posts).toHaveLength(2); expect(posts[0]).toBe(posts[1])
  expect(request.mock.calls.filter(([path]) => path.endsWith('?status='))).toHaveLength(2)
})

it('blocks unseen versions and migrates only on explicit review with transfer rationale reset', async () => {
  const old = { ...emptyReview(candidate), note: '旧版劳动保留', binding: 'old29', stage: 'far_transfer' as const,
    sameSkill: '旧版理由不能静默沿用', changedContext: '旧版情境不能静默沿用' }
  saveSpecializedReviews('child-a', readSpecializedReviews('child-a'), [old])
  vi.spyOn(api, 'requestParentLocal').mockResolvedValue(context)
  setup(); fireEvent.click(await screen.findByRole('button', { name: '核对当前原题' }))
  await screen.findByText(/草稿属于旧版本/)
  expect(screen.getByRole('button', { name: '批准' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '按当前原题重新核对' }))
  expect(readSpecializedReviews('child-a').value.entries[0]).toMatchObject({ note: old.note, binding: 'version29', stage: '', sameSkill: '', changedContext: '' })
})

it('cannot bind a fresh version to the old reference answer still displayed in the list', async () => {
  vi.spyOn(api, 'requestParentLocal').mockResolvedValue({ ...context, candidate_expected_answer: { ...candidate.expected_answer, exact_answers: ['7'] } })
  setup()
  fireEvent.change(await screen.findByLabelText('专项候选题1审核备注'), { target: { value: '只审核过原来的6' } })
  fireEvent.click(screen.getByRole('button', { name: '核对当前原题' }))
  await waitFor(() => expect(screen.getByRole('button', { name: '批准' })).toBeDisabled())
  await screen.findByText(/列表中的题目或参考答案已变化/)
  expect(readSpecializedReviews('child-a').value.entries[0].binding).toBe('')
})

it('submits an explicit source-bound stage only with original quotes and human rationales', async () => {
  let body: ReviewBody | undefined
  vi.spyOn(api, 'requestParentLocal').mockImplementation(async (_path, init) => {
    if (init?.method !== 'POST') return context
    body = JSON.parse(String(init.body)); return receipt(body!)
  })
  setup(); await draft()
  fireEvent.change(screen.getByLabelText('发布用途'), { target: { value: 'far_transfer' } })
  expect(screen.getByRole('button', { name: '批准' })).toBeDisabled()
  for (const [name, value] of [['原题依据原文', context.source_prompt], ['候选题依据原文', candidate.prompt],
    ['相同技能的核对理由', '两道题都必须枚举两个独立条件的搭配。'], ['条件或情境变化的核对理由', '从衣物搭配变成颜色与形状的组合条件。']]) {
    fireEvent.change(screen.getByLabelText(name), { target: { value } })
  }
  fireEvent.click(screen.getByRole('button', { name: '批准' })); await screen.findByText(/原审核已保存/)
  expect(body?.stage_review).toMatchObject({ binding_version: 'version29', stage: 'far_transfer', source_quote: context.source_prompt })
})

it('keeps legacy model keyword rows blocked, with original evidence visible', async () => {
  const request = vi.spyOn(api, 'requestParentLocal')
  render(<SpecializedReviewQueue childId="child-a" candidates={[{ ...candidate,
    validation: { passed: true, errors: [], ruleset: 'specialized_supply_local_gate_v1' }, expected_answer: { keywords: ['six'] } }]} onSaved={vi.fn()} />)
  expect(await screen.findByText(/旧版关键词候选/)).toBeInTheDocument()
  expect(screen.getByText('six')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '批准' })).toBeDisabled()
  expect(request).not.toHaveBeenCalled()
})

it.each(['integer', 'rational'])('shows the explicit %s numerical contract without upgrading text rules', async numericKind => {
  const numeric = { ...candidate, expected_answer: { ...candidate.expected_answer, answer_format: 'reviewed_spoken_numeric_v1', numeric_kind: numericKind } }
  vi.spyOn(api, 'requestParentLocal').mockResolvedValue({ ...context, candidate_expected_answer: numeric.expected_answer })
  render(<SpecializedReviewQueue childId="child-a" candidates={[numeric]} onSaved={vi.fn()} />)
  expect(await screen.findByText(numericKind === 'integer' ? '评分规则：单个整数等值。' : '评分规则：单个有理数等值。')).toBeInTheDocument()
  expect(screen.getByText(/单位、百分数、多答案、否定句和表达式不按本规则自动评分/)).toBeInTheDocument()
  await draft()
  expect(screen.getByRole('button', { name: '批准' })).toBeEnabled()
})

it.each(['throw', 'silent'])('never sends when local storage fails before dispatch (%s)', async mode => {
  const request = vi.spyOn(api, 'requestParentLocal').mockResolvedValue(context)
  setup(); await draft()
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { if (mode === 'throw') throw new Error('full') })
  fireEvent.click(screen.getByRole('button', { name: '批准' }))
  await screen.findByText(/本机审核记录没有安全保存/)
  expect(request.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(0)
})

it('does not overwrite corrupt or another-tab journals', () => {
  localStorage.setItem(specializedReviewKey('child-a'), '{')
  expect(() => readSpecializedReviews('child-a')).toThrow()
  expect(localStorage.getItem(specializedReviewKey('child-a'))).toBe('{')
  localStorage.clear()
  const before = readSpecializedReviews('child-a')
  saveSpecializedReviews('child-a', before, [{ ...emptyReview(candidate), note: 'other tab' }])
  expect(() => saveSpecializedReviews('child-a', before, [])).toThrow()
})

it('scopes pending recovery to the original child and refuses cross-child receipts', async () => {
  const body: ReviewBody = { decision: 'reject', note: 'A private note', client_action_id: 'original-child-a' }
  saveSpecializedReviews('child-a', readSpecializedReviews('child-a'), [{ ...emptyReview(candidate), note: body.note, pending: body }])
  render(<SpecializedReviewQueue childId="child-b" candidates={[candidate]} onSaved={vi.fn()} />)
  expect(screen.queryByText(candidate.prompt)).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '核对原审核' })).not.toBeInTheDocument()
  vi.spyOn(api, 'requestParentLocal').mockResolvedValue({ items: [{ ...receipt(body), child_id: 'child-b' }] })
  await expect(checkOriginalReview('child-a', candidate.candidate_id, body, new AbortController().signal)).rejects.toThrow()
  expect(readSpecializedReviews('child-a').value.entries[0].pending).toEqual(body)
})

it.each(['hsc-privacy-pause', specializedReviewCleared])('does not resurrect or acknowledge late results after %s', async event => {
  let resolve!: (value: unknown) => void
  vi.spyOn(api, 'requestParentLocal').mockImplementation((_path, init) => init?.method === 'POST'
    ? new Promise(r => { resolve = r }) : Promise.resolve(context))
  const done = vi.fn(); setup(done); await draft(); fireEvent.click(screen.getByRole('button', { name: '批准' }))
  await waitFor(() => expect(resolve).toBeTypeOf('function'))
  const pending = readSpecializedReviews('child-a').value.entries[0].pending!
  act(() => {
    if (event === specializedReviewCleared) localStorage.removeItem(specializedReviewKey('child-a'))
    window.dispatchEvent(new CustomEvent(event, { detail: 'child-a' }))
  })
  await act(async () => resolve(receipt(pending)))
  expect(done).not.toHaveBeenCalled()
  if (event === specializedReviewCleared) expect(localStorage.getItem(specializedReviewKey('child-a'))).toBeNull()
  else expect(readSpecializedReviews('child-a').value.entries[0].pending).toEqual(pending)
})

it('does not call a different winning key or missing candidate a saved review', async () => {
  const body: ReviewBody = { decision: 'reject', note: 'no', client_action_id: 'original-action29' }
  const request = vi.spyOn(api, 'requestParentLocal').mockResolvedValue({ items: [{ ...receipt(body), review_action_id: 'other-action29' }] })
  expect(await checkOriginalReview('child-a', candidate.candidate_id, body, new AbortController().signal)).toBe('conflict')
  request.mockResolvedValue({ items: [] })
  expect(await checkOriginalReview('child-a', candidate.candidate_id, body, new AbortController().signal)).toBe('conflict')
})
