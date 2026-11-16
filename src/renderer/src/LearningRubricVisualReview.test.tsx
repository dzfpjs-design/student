import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { LearningRubricReviewPanel } from './LearningRubricReviewPanel'
import type { RubricVisualEvidence } from './RubricVisualReview'
vi.mock('./RubricVisualReview', async original => ({ ...await original<typeof import('./RubricVisualReview')>(),
  RubricVisualReview: (p: { assessmentId: string; fingerprint: string; onEvidence: (v: RubricVisualEvidence) => void }) =>
    <button onClick={() => p.onEvidence({ assessment_id: p.assessmentId, assessment_fingerprint: p.fingerprint,
      binding_version: 'figure-version', displays: [{ material_key: 'figure', display_receipt_id: 'display' }] })}>synthetic display receipt</button> }))
const child = { child_id: 'child', nickname: '孩子', grade: '初三' }, flags = { local_only: true, cloud_transmission_allowed: false }
function fixture() {
  let revision = 0, withdrawn = false, fail = false
  const history: unknown[] = [], posts: Record<string, unknown>[] = []
  const assessment = () => ({ ...flags, assessment_id: 'assessment', child_id: 'child', fingerprint: 'original-answer', question: '解释图形',
    response: '封闭图形', subject: '物理', profile: 'school_open_science', source_kind: 'model_candidate', source_status: 'current',
    score_status: revision ? (withdrawn ? 'withdrawn' : 'reviewed_human') : 'candidate', review_revision: revision,
    review: history.at(-1) ?? null, criteria: ['识别'], responded_at: '2026-09-10T10:00:00Z',
    dimensions: [{ dimension_id: 'answer', criterion: '识别图形', max_score: 2, pass_score: 1, score_anchors: {}, score: null, response_quotes: [], rationale: 'not_assessed' }],
    material: { status: 'available', stimulus_text: '图形材料', visual: { ...flags, version: 'rubric_visual_material_v1', status: 'current',
      source_current: true, image_availability: 'available', binding_version: 'figure-version', reason: null,
      materials: [{ material_key: 'figure', binding: {}, image_descriptor: {} }] } } })
  const detail = () => ({ ...flags, assessment: assessment(), history, qualification: { ...flags, assessment_id: 'assessment',
    eligible_for_mastery_gate: false, automatic_certification: false, receipt: revision ? { assessment_id: 'assessment', review_revision: revision } : null } })
  const request = vi.fn(async (path: string, init?: RequestInit) => {
    if (!init?.method) return path.includes('?') ? { ...flags, child_id: 'child', items: [assessment()], total: 1, offset: 0 } : detail()
    const body = JSON.parse(String(init.body)); posts.push(body)
    if (fail) throw Error('response lost')
    revision++; withdrawn = body.operation === 'retract'
    const event = { ...flags, ...body, assessment_id: 'assessment', review_revision: revision, score_status: withdrawn ? 'withdrawn' : 'reviewed_human', created_at: '2026-09-10T10:01:00Z', reviewer_role: 'parent', passed: true }
    history.push(event)
    return { ...detail(), event }
  })
  return { request, posts, fail: () => { fail = true } }
}
function fill() {
  for (const [label, value] of [['结果评分', '1'], ['结果回答摘录', '封闭图形'], ['结果评分理由', '核对了原图'], ['目标1回答摘录', '封闭图形'],
    ['独立完成情况', 'yes'], ['提示等级', '0'], ['审核说明', '核对本次原图和回答']]) fireEvent.change(screen.getByLabelText(label), { target: { value } })
}
afterEach(() => { cleanup(); localStorage.clear() })
it('does not approve a figure from text alone; freezes displayed evidence with the original pending operation', async () => {
  const f = fixture(); f.fail()
  render(<LearningRubricReviewPanel child={child} request={f.request} />)
  await screen.findByLabelText('结果评分'); fill()
  const approve = screen.getByRole('button', { name: '保存评分审核' })
  expect(approve).toBeDisabled()
  fireEvent.click(screen.getByText('synthetic display receipt'))
  await waitFor(() => expect(approve).toBeEnabled())
  fireEvent.click(approve)
  await screen.findByText('重试原审核')
  expect(f.posts[0].visual_evidence).toEqual({ assessment_id: 'assessment', assessment_fingerprint: 'original-answer', binding_version: 'figure-version', displays: [{ material_key: 'figure', display_receipt_id: 'display' }] })
  cleanup()
  render(<LearningRubricReviewPanel child={child} request={f.request} />)
  fireEvent.click(await screen.findByText('重试原审核'))
  await waitFor(() => expect(f.posts).toHaveLength(2))
  expect(f.posts[1]).toEqual(f.posts[0])
})
