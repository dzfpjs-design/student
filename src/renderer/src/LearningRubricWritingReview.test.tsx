import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './api'
import { LearningRubricReviewPanel } from './LearningRubricReviewPanel'

const child = { child_id: 'child-a', nickname: '孩子甲' }
const local = { local_only: true as const, cloud_transmission_allowed: false as const }
function fixture(profile = 'school_open_written_v1', subject = '物理') {
  const ids = subject === '物理' ? ['claim', 'evidence', 'reasoning', 'limitations'] : subject === '数学' ? ['answer', 'method', 'reasoning'] : ['task', 'coherence', 'language']
  let assessment: any = { ...local, assessment_id: 'a1', child_id: child.child_id, fingerprint: 'fp1', question: '说明条件与推理过程。',
    response: '我还没有说清楚条件。', subject, profile: profile === 'pet_writing_text_v1' ? 'english_open' : 'school_open_scientific_explanation_v1',
    source_kind: 'local_open_review', source_status: 'current', score_status: 'candidate', review_revision: 0, review: null,
    dimensions: ids.map(id => ({ dimension_id: id, criterion: `${id}标准`, max_score: 3, pass_score: 2,
      score_anchors: { '0': '未达到', '1': '部分', '2': '符合', '3': '完整' }, score: null, response_quotes: [], rationale: 'not_assessed' })),
    criteria: ['用原回答解释目标'], responded_at: '2026-09-08T00:00:00Z', material: { status: 'current', diagnostic_item_id: 'item1' } }
  let binding: any = { ...local, profile, assessment_id: 'a1', assessment_fingerprint: 'fp1', binding_version: 'binding1', diagnostic_item_id: 'item1',
    prompt_id: 'prompt1', item_version: 'version1', attempt_id: 'attempt1', evidence_id: 'evidence1', knowledge_node_id: 'node1',
    original: { ...local, schema: 'writing_original_v1', artifact_id: 'original1', kind: 'confirmed_document', source_version: 'original-version1', response_hash: 'hash1' },
    task_points: ['说明原题条件', '给出推理依据'] }
  const history: any[] = [], receipts = new Map<string, any>()
  let lose = false
  const detail = () => ({ ...local, assessment: structuredClone(assessment), history: structuredClone(history),
    writing_review: { ...local, required: true, profile, binding: structuredClone(binding), blockers: binding ? [] : ['WRITING_ORIGINAL_UNAVAILABLE'] },
    qualification: { ...local, assessment_id: 'a1', eligible_for_mastery_gate: false, automatic_certification: false, receipt: null } })
  const request = vi.fn(async (path: string, init?: RequestInit): Promise<unknown> => {
    if (!init?.method) return path.includes('?') ? { ...local, child_id: child.child_id, items: [assessment], total: 1, offset: 0 } : detail()
    const body = JSON.parse(String(init.body))
    if (!receipts.has(body.client_action_id)) {
      if (body.expected_revision !== assessment.review_revision) throw new ApiError('conflict', 409)
      const event = { ...local, ...body, assessment_id: 'a1', review_revision: assessment.review_revision + 1, created_at: '2026-09-08T00:01:00Z',
        reviewer_role: 'parent', score_status: body.operation === 'retract' ? 'withdrawn' : 'reviewed_human', passed: false }
      history.push(event); receipts.set(body.client_action_id, event)
      assessment = { ...assessment, review_revision: event.review_revision, review: event, score_status: event.score_status }
    }
    if (lose) { lose = false; throw new TypeError('lost response') }
    return { ...detail(), event: receipts.get(body.client_action_id) }
  })
  return { request, get: () => assessment, setMaterial: (text: string) => { assessment.material.stimulus_text = text },
    setBinding: (b: any) => { binding = b }, binding: () => binding, lose: () => { lose = true } }
}
function fill() {
  for (const fieldset of document.querySelectorAll('.rubric-review fieldset')) {
    fireEvent.change(fieldset.querySelector('select')!, { target: { value: '0' } })
    for (const field of fieldset.querySelectorAll('textarea')) {
      const label = field.closest('label')!.textContent!
      fireEvent.change(field, { target: { value: label.includes('材料') ? '' : label.includes('理由') ? '原稿未说明该标准所需依据。' : '我还没有说清楚条件。' } })
    }
  }
  fireEvent.change(screen.getByLabelText('目标1回答摘录'), { target: { value: '我还没有说清楚条件。' } })
  fireEvent.change(screen.getByLabelText('独立完成情况'), { target: { value: 'no' } })
  fireEvent.change(screen.getByLabelText('提示等级'), { target: { value: '1' } })
  fireEvent.change(screen.getByLabelText('审核说明'), { target: { value: '按本题原稿核对，保留低分反馈，不认证掌握。' } })
}
describe('Original written review profiles', () => {
  afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks() })
  it('preserves exact material evidence alongside original task-point binding through lost response and remount', async () => {
    const f = fixture(); f.setMaterial('甲的电阻为十欧姆，乙的电阻为二十欧姆。'); f.lose()
    const first = render(<LearningRubricReviewPanel child={child} request={f.request} />)
    await screen.findByLabelText('审核说明'); fill()
    for (let i = 1; i <= 2; i++) fireEvent.change(screen.getByLabelText(`原稿任务点${i}摘录`), { target: { value: f.get().response } })
    fireEvent.change(screen.getByLabelText('文本依据评分'), { target: { value: '1' } })
    expect(screen.getByRole('button', { name: '保存评分审核' })).toBeDisabled()
    expect(screen.getByLabelText('文本依据材料摘录')).toHaveAttribute('aria-required', 'true')
    fireEvent.change(screen.getByLabelText('文本依据材料摘录'), { target: { value: '甲的电阻为十欧姆' } })
    fireEvent.click(screen.getByRole('button', { name: '保存评分审核' })); await screen.findByText(/请求结果尚未核对/)
    const posts = () => f.request.mock.calls.filter(([, init]) => init?.method)
    const original = posts()[0]; const body = JSON.parse(String(original[1]!.body))
    expect(body.review_profile).toBe('school_open_written_v1')
    expect(body.dimensions.find((d: any) => d.dimension_id === 'evidence').source_quotes).toEqual(['甲的电阻为十欧姆'])
    expect(body.writing_evidence.binding_version).toBe('binding1')
    expect(body.writing_evidence.task_point_evidence).toHaveLength(2)
    first.unmount()
    render(<LearningRubricReviewPanel child={child} request={f.request} />)
    await screen.findByText('第1次 · 人工评分审核'); expect(posts()).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: '重试原审核' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: '重试原审核' })).not.toBeInTheDocument())
    expect(posts()[1]).toEqual(original)
    expect(screen.getByText('材料依据：甲的电阻为十欧姆')).toBeInTheDocument()
  })
  it.each([['school_open_written_v1', '物理'], ['school_open_written_v1', '数学'], ['pet_writing_text_v1', '英语']])('submits %s with exact original and all task points for %s', async (profile, subject) => {
    const f = fixture(profile, subject)
    render(<LearningRubricReviewPanel child={child} request={f.request} />)
    await screen.findByLabelText('审核说明'); fill()
    expect(screen.getByRole('button', { name: '保存评分审核' })).toBeDisabled()
    for (let i = 1; i <= 2; i++) fireEvent.change(screen.getByLabelText(`原稿任务点${i}摘录`), { target: { value: f.get().response } })
    fireEvent.click(screen.getByRole('button', { name: '保存评分审核' }))
    await screen.findByText('第1次 · 人工评分审核')
    const body = JSON.parse(String(f.request.mock.calls.find(([, init]) => init?.method)?.[1]?.body))
    expect(body.review_profile).toBe(profile)
    expect(body.writing_evidence).toEqual({ binding_version: 'binding1', task_point_evidence: f.binding().task_points.map((task_point: string) => ({ task_point, response_quote: f.get().response })) })
    expect(body.dimensions.map((d: any) => d.dimension_id)).toEqual(f.get().dimensions.map((d: any) => d.dimension_id))
    expect(body.dimensions.every((d: any) => d.score === 0)).toBe(true)
    expect(body).not.toHaveProperty('reviewer_id'); expect(body).not.toHaveProperty('audio_evidence')
    expect(screen.getByText('这份评分当前不能用于独立掌握门槛。')).toBeInTheDocument()
  })
  it('blocks missing original binding even when generic dimensions and criteria are filled', async () => {
    const f = fixture(); f.setBinding(null)
    render(<LearningRubricReviewPanel child={child} request={f.request} />)
    await screen.findByLabelText('审核说明'); fill()
    expect(screen.getByRole('button', { name: '保存评分审核' })).toBeDisabled()
    expect(screen.getByText(/缺少当前原稿与本次作答的有效绑定/)).toBeInTheDocument()
  })
  it('keeps lost-response review exact across remount and never auto-sends', async () => {
    const f = fixture(); f.lose()
    const first = render(<LearningRubricReviewPanel child={child} request={f.request} />)
    await screen.findByLabelText('审核说明'); fill()
    for (let i = 1; i <= 2; i++) fireEvent.change(screen.getByLabelText(`原稿任务点${i}摘录`), { target: { value: f.get().response } })
    fireEvent.click(screen.getByRole('button', { name: '保存评分审核' })); await screen.findByText(/请求结果尚未核对/)
    const posts = () => f.request.mock.calls.filter(([, init]) => init?.method)
    const original = posts()[0]; first.unmount()
    render(<LearningRubricReviewPanel child={child} request={f.request} />)
    await screen.findByText('第1次 · 人工评分审核'); expect(posts()).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: '重试原审核' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: '重试原审核' })).not.toBeInTheDocument())
    expect(posts()[1]).toEqual(original); expect(f.get().review_revision).toBe(1)
  })
  it('rejects current binding pointing at a different assessment or original item', async () => {
    const f = fixture(); f.setBinding({ ...f.binding(), diagnostic_item_id: 'other-item' })
    render(<LearningRubricReviewPanel child={child} request={f.request} />)
    await screen.findByRole('alert')
    expect(screen.queryByRole('button', { name: '保存评分审核' })).not.toBeInTheDocument()
    expect(f.request.mock.calls.every(([, init]) => !init?.method)).toBe(true)
  })
  it('never substitutes a known profile for an unknown original review profile', async () => {
    const f = fixture('future_written_v2')
    render(<LearningRubricReviewPanel child={child} request={f.request} />)
    await screen.findByLabelText('审核说明'); fill()
    expect(screen.getByRole('button', { name: '保存评分审核' })).toBeDisabled()
    expect(screen.getByText(/审核类型尚不支持/)).toBeInTheDocument()
  })
  it('does not carry original quotes to a revised binding and retracts without a replacement score', async () => {
    const f = fixture()
    const first = render(<LearningRubricReviewPanel child={child} request={f.request} />)
    await screen.findByLabelText('审核说明'); fill()
    for (let i = 1; i <= 2; i++) fireEvent.change(screen.getByLabelText(`原稿任务点${i}摘录`), { target: { value: f.get().response } })
    first.unmount(); f.setBinding({ ...f.binding(), binding_version: 'binding2' })
    render(<LearningRubricReviewPanel child={child} request={f.request} />)
    await screen.findByText(/本机草稿绑定旧审核版本/)
    expect(screen.getByRole('button', { name: '保存评分审核' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '放弃旧草稿并核对新版' })); fill()
    expect(screen.getByLabelText('原稿任务点1摘录')).toHaveValue('')
    for (let i = 1; i <= 2; i++) fireEvent.change(screen.getByLabelText(`原稿任务点${i}摘录`), { target: { value: f.get().response } })
    fireEvent.click(screen.getByRole('button', { name: '保存评分审核' })); await screen.findByText('第1次 · 人工评分审核')
    fireEvent.change(screen.getByLabelText('审核说明'), { target: { value: '需重新核对本次审核。' } })
    fireEvent.click(screen.getByRole('button', { name: '撤回评分审核' })); await screen.findByText('第2次 · 撤回评分')
    const posts = f.request.mock.calls.filter(([, init]) => init?.method).map(([, init]) => JSON.parse(String(init?.body)))
    expect(posts[0].writing_evidence.binding_version).toBe('binding2')
    expect(posts[1]).toEqual({ expected_revision: 1, client_action_id: expect.any(String), operation: 'retract', reason: '需重新核对本次审核。' })
    expect(screen.getByText('第1次 · 人工评分审核')).toBeInTheDocument()
  })
})
