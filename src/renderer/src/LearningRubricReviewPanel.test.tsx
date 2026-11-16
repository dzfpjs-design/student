import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './api'
import { LearningRubricReviewPanel, clearLearningRubricLocalState } from './LearningRubricReviewPanel'

const child = { child_id: 'child-a', nickname: '孩子甲', grade: '四年级' }
const local = { local_only: true, cloud_transmission_allowed: false }
function fixture(mapping = false) {
  let assessment = { ...local, assessment_id: 'assessment-a', child_id: child.child_id, fingerprint: 'frozen-answer-v1',
    question: '先算括号，说明过程。', response: '先算24+6=30，再算30*3=90。', subject: '数学',
    source_kind: mapping ? 'local_criteria_mapping' : 'model_candidate', profile: mapping ? 'local_criteria_mapping' : 'math_process',
    source_status: 'current', score_status: 'candidate', review_revision: 0, review: null as any,
    criteria: ['正确解释运算顺序'], material: { status: 'unbound' }, responded_at: '2026-09-07T10:00:00Z',
    dimensions: mapping ? [] : [{ dimension_id: 'answer', criterion: '结果与条件符合题意', max_score: 3, pass_score: 2,
      score_anchors: { '0': '缺少证据', '1': '部分满足', '2': '符合要求', '3': '充分准确' }, score: 3, response_quotes: ['30*3=90'], rationale: '模型候选理由' }] }
  const history: any[] = []
  const detail = () => ({ ...local, assessment, history, qualification: { ...local, assessment_id: assessment.assessment_id, eligible_for_mastery_gate: assessment.score_status === 'reviewed_human', automatic_certification: false, receipt: assessment.score_status === 'reviewed_human' ? { assessment_id: assessment.assessment_id, review_revision: assessment.review_revision } : null } })
  const request = vi.fn(async (path: string, init?: RequestInit): Promise<unknown> => {
    if (!init?.method) return path.includes('?') ? { ...local, child_id: child.child_id, items: [assessment], total: 1, offset: 0 } : detail()
    const body = JSON.parse(String(init.body))
    if (body.expected_revision !== assessment.review_revision) throw new ApiError('版本冲突', 409, 'RUBRIC_REVISION_CONFLICT')
    const event = { ...local, ...body, assessment_id: assessment.assessment_id, review_revision: assessment.review_revision + 1, created_at: '2026-09-07T10:00:01Z', score_status: body.operation === 'retract' ? 'withdrawn' : 'reviewed_human', reviewer_role: 'parent', passed: true }
    history.push(event)
    assessment = { ...assessment, score_status: event.score_status, review_revision: event.review_revision, review: event }
    return { ...detail(), event }
  })
  return { request, get: () => assessment, change: (values: Record<string, unknown>) => { assessment = { ...assessment, ...values } as typeof assessment } }
}
function conditions() {
  fireEvent.change(screen.getByLabelText('独立完成情况'), { target: { value: 'yes' } })
  fireEvent.change(screen.getByLabelText('提示等级'), { target: { value: '0' } })
  fireEvent.change(screen.getByLabelText('审核说明'), { target: { value: '核对原回答与现场条件。' } })
  fireEvent.change(screen.getByLabelText('目标1回答摘录'), { target: { value: '24+6=30' } })
}
function score() {
  fireEvent.change(screen.getByLabelText('结果评分'), { target: { value: '2' } })
  fireEvent.change(screen.getByLabelText('结果回答摘录'), { target: { value: '30*3=90' } })
  fireEvent.change(screen.getByLabelText('结果评分理由'), { target: { value: '结果可由原回答追溯。' } })
}

describe('LearningRubricReviewPanel', () => {
  afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks() })
  it('shows current calibrated scoring separately from mastery and removes it after revocation', async () => {
    const f = fixture()
    let current = true
    const request = vi.fn(async (path: string, init?: RequestInit) => {
      const value = await f.request(path, init) as any
      if (value.qualification && current) value.qualification = { ...value.qualification,
        score_qualified: true, eligible_for_mastery_gate: false,
        receipt: { assessment_id: 'assessment-a', review_revision: 0, score_status: 'calibrated_model' } }
      if (value.items && current) value.items = [...value.items, { ...value.items[0], assessment_id: 'assessment-b' }].map((item: any) => ({ ...item,
        qualification: { ...local, assessment_id: item.assessment_id, automatic_certification: false,
          score_qualified: true, eligible_for_mastery_gate: false,
          receipt: { assessment_id: item.assessment_id, review_revision: 0, score_status: 'calibrated_model' } } }))
      return value
    })
    const view = render(<LearningRubricReviewPanel child={child} request={request} />)
    await screen.findByText('本次模型评分已通过校准核验。')
    expect(view.container.querySelector('.rr-status')).toHaveTextContent('已校准')
    expect(view.container.querySelectorAll('.rr-status')).toHaveLength(2)
    for (const status of view.container.querySelectorAll('.rr-status')) expect(status).toHaveTextContent('已校准')
    expect(f.get().score_status).toBe('candidate')
    expect(screen.getByText('这份评分当前不能用于独立掌握门槛。')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '待人工审核' })).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('审核说明'), { target: { value: '尚未提交的家长核对意见' } })
    current = false
    view.unmount()
    const reopened = render(<LearningRubricReviewPanel child={child} request={request} />)
    await screen.findByRole('heading', { name: '待人工审核' })
    expect(reopened.container.querySelector('.rr-status')).toHaveTextContent('待核对')
    expect(screen.queryByText('本次模型评分已通过校准核验。')).not.toBeInTheDocument()
    expect(screen.getByLabelText('审核说明')).toHaveValue('尚未提交的家长核对意见')
    expect(request.mock.calls.every(([, init]) => !init?.method)).toBe(true)
  })
  it('keeps explicit parent correction available for an already qualified score', async () => {
    const f = fixture()
    const request = vi.fn(async (path: string, init?: RequestInit) => {
      const value = await f.request(path, init) as any
      if (value.qualification && value.assessment.review_revision === 0) value.qualification = { ...value.qualification,
        score_qualified: true, eligible_for_mastery_gate: false,
        receipt: { assessment_id: 'assessment-a', review_revision: 0, score_status: 'calibrated_model' } }
      return value
    })
    render(<LearningRubricReviewPanel child={child} request={request} />)
    await screen.findByRole('heading', { name: '家长复核与纠正' })
    conditions(); score()
    fireEvent.click(screen.getByRole('button', { name: '保存评分审核' }))
    await screen.findByRole('heading', { name: '已人工审核' })
    expect(f.get().review_revision).toBe(1)
    expect(request.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
  })
  it('lets newer detail withdrawal override a stale calibrated list entry', async () => {
    const f = fixture()
    const request = vi.fn(async (path: string, init?: RequestInit) => {
      const value = await f.request(path, init) as any
      if (value.items) value.items = value.items.map((item: any) => ({ ...item,
        qualification: { ...local, assessment_id: item.assessment_id, automatic_certification: false,
          score_qualified: true, eligible_for_mastery_gate: false,
          receipt: { assessment_id: item.assessment_id, review_revision: 0, score_status: 'calibrated_model' } } }))
      return value
    })
    const view = render(<LearningRubricReviewPanel child={child} request={request} />)
    await screen.findByLabelText('审核说明')
    expect(view.container.querySelector('.rr-status')).toHaveTextContent('待核对')
    expect(screen.queryByText('本次模型评分已通过校准核验。')).not.toBeInTheDocument()
  })
  it.each(['foreign-receipt', 'wrong-revision', 'automatic-certification'])('rejects malformed list authority: %s', async failure => {
    const f = fixture()
    const request = vi.fn(async (path: string, init?: RequestInit) => {
      const value = await f.request(path, init) as any
      if (value.items) value.items = value.items.map((item: any) => ({ ...item,
        qualification: { ...local, assessment_id: item.assessment_id,
          automatic_certification: failure === 'automatic-certification', score_qualified: true,
          eligible_for_mastery_gate: false, receipt: {
            assessment_id: failure === 'foreign-receipt' ? 'other-assessment' : item.assessment_id,
            review_revision: failure === 'wrong-revision' ? 1 : 0, score_status: 'calibrated_model' } } }))
      return value
    })
    render(<LearningRubricReviewPanel child={child} request={request} />)
    await screen.findByText('原回答或审核版本已变化。请核对最新记录，旧决定不会自动用于新版本。')
    expect(screen.queryByText(/已校准/)).not.toBeInTheDocument()
    expect(screen.queryByLabelText('审核说明')).not.toBeInTheDocument()
    expect(request.mock.calls.every(([, init]) => !init?.method)).toBe(true)
  })
  it.each(['unknown', 'changed-source', 'unqualified'])('does not promote %s to calibrated scoring', async failure => {
    const f = fixture()
    if (failure === 'changed-source') f.change({ source_status: 'changed' })
    const request = vi.fn(async (path: string, init?: RequestInit) => {
      const value = await f.request(path, init) as any
      if (value.qualification) value.qualification = { ...value.qualification,
        score_qualified: failure !== 'unqualified',
        receipt: { assessment_id: 'assessment-a', review_revision: 0,
          score_status: failure === 'unknown' ? 'unknown_future_status' : 'calibrated_model' } }
      return value
    })
    render(<LearningRubricReviewPanel child={child} request={request} />)
    await screen.findByLabelText('审核说明')
    expect(screen.queryByText('本次模型评分已通过校准核验。')).not.toBeInTheDocument()
    expect(screen.getByText('这份评分当前不能用于独立掌握门槛。')).toBeInTheDocument()
    expect(request.mock.calls.every(([, init]) => !init?.method)).toBe(true)
  })
  it('focuses the exact off-page assessment and preserves both drafts across restart without submitting', async () => {
    const a = fixture(); const b = fixture()
    b.change({ assessment_id: 'assessment-b', question: '另一个原题' })
    const request = vi.fn((path: string, init?: RequestInit) => path.endsWith('/assessment-b') ? b.request(path, init) : a.request(path, init))
    const onFocusResolved = vi.fn()
    const view = render(<LearningRubricReviewPanel child={child} request={request} />)
    await screen.findByLabelText('审核说明')
    fireEvent.change(screen.getByLabelText('审核说明'), { target: { value: '甲题未提交草稿' } })
    view.rerender(<LearningRubricReviewPanel child={child} request={request} focusRequest={{ objectId: 'assessment-b', nonce: 'b' }} onFocusResolved={onFocusResolved} />)
    await waitFor(() => expect(onFocusResolved).toHaveBeenCalledWith('b'))
    expect(document.activeElement).toHaveAttribute('data-work-object', 'assessment-b')
    expect(screen.getByLabelText('审核说明')).toHaveValue('')
    fireEvent.change(screen.getByLabelText('审核说明'), { target: { value: '乙题未提交草稿' } })
    view.unmount()
    render(<LearningRubricReviewPanel child={child} request={request} focusRequest={{ objectId: 'assessment-a', nonce: 'a' }} onFocusResolved={onFocusResolved} />)
    await waitFor(() => expect(onFocusResolved).toHaveBeenCalledWith('a'))
    expect(screen.getByLabelText('审核说明')).toHaveValue('甲题未提交草稿')
    expect(JSON.parse(localStorage.getItem('hsc-learning-rubric-review:child-a')!).drafts['assessment-b'].reason).toBe('乙题未提交草稿')
    expect(request.mock.calls.every(([, init]) => !init?.method)).toBe(true)
  })
  it.each(['forbidden', 'missing', 'wrong-child', 'withdrawn'])('rejects exact focus %s without substituting the first assessment', async failure => {
    const f = fixture(); const target = fixture()
    target.change({ assessment_id: 'assessment-b', ...(failure === 'wrong-child' ? { child_id: 'child-b' } : {}), ...(failure === 'withdrawn' ? { score_status: 'withdrawn' } : {}) })
    const request = vi.fn((path: string, init?: RequestInit) => path.endsWith('/assessment-b')
      ? failure === 'forbidden' || failure === 'missing' ? Promise.reject(new ApiError('不可读取', failure === 'forbidden' ? 403 : 404)) : target.request(path, init)
      : f.request(path, init))
    const done = vi.fn()
    render(<LearningRubricReviewPanel child={child} request={request} focusRequest={{ objectId: 'assessment-b', nonce: failure }} onFocusResolved={done} />)
    await waitFor(() => expect(done).toHaveBeenCalledWith(failure, expect.any(String)))
    expect(done).toHaveBeenCalledTimes(1)
    expect(document.querySelector('[data-work-object="assessment-a"]')).toBeNull()
    expect(request.mock.calls.some(([path]) => path.endsWith('/assessment-a'))).toBe(false)
  })
  it('ignores a late old deep-link response and acknowledges only the current exact evidence', async () => {
    const a = fixture(); const b = fixture(); const c = fixture()
    b.change({ assessment_id: 'assessment-b' }); c.change({ assessment_id: 'assessment-c' })
    let release!: (value: unknown) => void
    const delayed = new Promise(resolve => { release = resolve })
    const request = vi.fn((path: string, init?: RequestInit) => path.endsWith('/assessment-b') ? delayed : path.endsWith('/assessment-c') ? c.request(path, init) : a.request(path, init))
    const done = vi.fn()
    const view = render(<LearningRubricReviewPanel child={child} request={request} focusRequest={{ objectId: 'assessment-b', nonce: 'old' }} onFocusResolved={done} />)
    await waitFor(() => expect(request.mock.calls.some(([path]) => path.endsWith('/assessment-b'))).toBe(true))
    view.rerender(<LearningRubricReviewPanel child={child} request={request} focusRequest={{ objectId: 'assessment-c', nonce: 'current' }} onFocusResolved={done} />)
    await waitFor(() => expect(done).toHaveBeenCalledWith('current'))
    await act(async () => release(await b.request('/assessment-b')))
    expect(done).toHaveBeenCalledTimes(1)
    expect(document.activeElement).toHaveAttribute('data-work-object', 'assessment-c')
    expect(document.querySelector('[data-work-object="assessment-b"]')).toBeNull()
  })
  it('preserves an unresolved original review when a deep link selects a different assessment', async () => {
    const a = fixture(true); const b = fixture(true)
    b.change({ assessment_id: 'assessment-b' })
    const request = vi.fn((path: string, init?: RequestInit) => init?.method ? Promise.reject(new ApiError('回执未知'))
      : path.endsWith('/assessment-b') ? b.request(path, init) : a.request(path, init))
    const done = vi.fn()
    const view = render(<LearningRubricReviewPanel child={child} request={request} />)
    await screen.findByLabelText('审核说明'); conditions()
    fireEvent.click(screen.getByRole('button', { name: '保存评分审核' }))
    await screen.findByRole('alert')
    const pending = JSON.parse(localStorage.getItem('hsc-learning-rubric-review:child-a')!).drafts['assessment-a'].pending
    view.rerender(<LearningRubricReviewPanel child={child} request={request} focusRequest={{ objectId: 'assessment-b', nonce: 'b' }} onFocusResolved={done} />)
    await waitFor(() => expect(done).toHaveBeenCalledWith('b'))
    expect(JSON.parse(localStorage.getItem('hsc-learning-rubric-review:child-a')!).drafts['assessment-a'].pending).toEqual(pending)
    expect(request.mock.calls.filter(([, init]) => init?.method)).toHaveLength(1)
    expect(screen.getByRole('button', { name: '查看另一份待核对审核' })).toBeInTheDocument()
  })
  it('submits actual empty dimensions for local mapping and persists review/retraction across remount', async () => {
    const f = fixture(true)
    const view = render(<LearningRubricReviewPanel child={child} request={f.request} />)
    await screen.findByLabelText('目标1回答摘录')
    expect(screen.queryByLabelText('结果评分')).not.toBeInTheDocument()
    conditions()
    fireEvent.click(screen.getByRole('button', { name: '保存评分审核' }))
    await screen.findByText('已人工审核')
    const body = JSON.parse(String(f.request.mock.calls.find(([, init]) => init?.method === 'POST')![1]!.body))
    expect(body.dimensions).toEqual([])
    expect(body.reviewer_id).toBeUndefined()
    view.unmount(); render(<LearningRubricReviewPanel child={child} request={f.request} />)
    await screen.findByText('核对原回答与现场条件。')
    fireEvent.change(screen.getByLabelText('审核说明'), { target: { value: '撤回，孩子实际有提示。' } })
    fireEvent.click(screen.getByRole('button', { name: '撤回评分审核' }))
    await screen.findByText('评分审核已撤回')
    const last = JSON.parse(String(f.request.mock.calls.filter(([, init]) => init?.method === 'POST').at(-1)![1]!.body))
    expect(last).toMatchObject({ expected_revision: 1, operation: 'retract' })
    expect(last).not.toHaveProperty('dimensions')
    expect(last).not.toHaveProperty('independent')
  })
  it('does not prefill candidate scores or unknown independence and rejects invented quotes', async () => {
    const f = fixture()
    render(<LearningRubricReviewPanel child={child} request={f.request} />)
    expect(await screen.findByLabelText('结果评分')).toHaveValue('')
    expect(screen.getByLabelText('独立完成情况')).toHaveValue('')
    conditions(); score()
    fireEvent.change(screen.getByLabelText('结果回答摘录'), { target: { value: '孩子从未说过的原话' } })
    expect(screen.getByRole('button', { name: '保存评分审核' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('结果回答摘录'), { target: { value: '30*3=90' } })
    fireEvent.click(screen.getByRole('button', { name: '保存评分审核' }))
    await screen.findByText('已人工审核')
    expect(f.request.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
  })
  it('keeps exact pending request through reload and never sends a replacement action', async () => {
    const f = fixture(true); const original = f.request.getMockImplementation()!
    let fail = true
    f.request.mockImplementation((path, init) => init?.method === 'POST' && fail ? Promise.reject(new ApiError('断线')) : original(path, init))
    const view = render(<LearningRubricReviewPanel child={child} request={f.request} />)
    await screen.findByLabelText('目标1回答摘录'); conditions()
    fireEvent.click(screen.getByRole('button', { name: '保存评分审核' }))
    await screen.findByRole('button', { name: '重试原审核' })
    const body = f.request.mock.calls.find(([, init]) => init?.method === 'POST')![1]!.body
    view.unmount(); fail = false
    render(<LearningRubricReviewPanel child={child} request={f.request} />)
    fireEvent.click(await screen.findByRole('button', { name: '重试原审核' }))
    await screen.findByText('已人工审核')
    expect(f.request.mock.calls.filter(([, init]) => init?.method === 'POST')[1][1]!.body).toEqual(body)
  })
  it('refreshes 409 without applying old inputs to the new review revision', async () => {
    const f = fixture(true)
    render(<LearningRubricReviewPanel child={child} request={f.request} />)
    await screen.findByLabelText('目标1回答摘录'); conditions()
    f.change({ review_revision: 1 })
    fireEvent.click(screen.getByRole('button', { name: '保存评分审核' }))
    await screen.findByText(/审核版本已变化/)
    expect(f.request.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
    expect(screen.getByRole('button', { name: '保存评分审核' })).toBeDisabled()
  })
  it('uses the auth callback and erases mounted private drafts on host deletion', async () => {
    const f = fixture(true); const original = f.request.getMockImplementation()!; const authneeded = vi.fn()
    f.request.mockImplementation((path, init) => init?.method === 'POST' ? Promise.reject(new ApiError('失效', 401)) : original(path, init))
    render(<LearningRubricReviewPanel child={child} request={f.request} authneeded={authneeded} />)
    await screen.findByLabelText('目标1回答摘录'); conditions()
    fireEvent.click(screen.getByRole('button', { name: '保存评分审核' }))
    fireEvent.click(await screen.findByRole('button', { name: '重新解锁家长操作' }))
    expect(authneeded).toHaveBeenCalledOnce()
    act(() => clearLearningRubricLocalState(child.child_id))
    await screen.findByLabelText('目标1回答摘录')
    expect(screen.getByLabelText('审核说明')).toHaveValue('')
    expect(screen.queryByRole('button', { name: '重试原审核' })).not.toBeInTheDocument()
  })

  it.each(['chinese_open', 'school_open_mathematical_process_v1', 'school_open_scientific_explanation_v1'])('requires exact material evidence for %s and submits it without borrowing answer quotes', async profile => {
    const f = fixture()
    f.change({ profile, subject: profile === 'chinese_open' ? '语文' : '物理', material: { status: 'bound', stimulus_text: '原文描写他把伞递给了同伴。' },
      dimensions: [{ ...f.get().dimensions[0], dimension_id: 'evidence', criterion: '原文依据支持判断' }] })
    render(<LearningRubricReviewPanel child={child} request={f.request} />)
    await screen.findByLabelText('文本依据评分'); conditions()
    fireEvent.change(screen.getByLabelText('文本依据评分'), { target: { value: '2' } })
    fireEvent.change(screen.getByLabelText('文本依据回答摘录'), { target: { value: '24+6=30' } })
    fireEvent.change(screen.getByLabelText('文本依据评分理由'), { target: { value: '核对依据' } })
    expect(screen.getByRole('button', { name: '保存评分审核' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('文本依据材料摘录'), { target: { value: '孩子没有说的文本' } })
    expect(screen.getByRole('button', { name: '保存评分审核' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('文本依据材料摘录'), { target: { value: '把伞递给了同伴' } })
    expect(screen.getByRole('button', { name: '保存评分审核' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: '保存评分审核' }))
    await waitFor(() => expect(f.get().review_revision).toBe(1))
    const body = JSON.parse(String(f.request.mock.calls.find(([, init]) => init?.method === 'POST')![1]!.body))
    expect(body.dimensions[0]).toMatchObject({ dimension_id: 'evidence', score: 2,
      response_quotes: ['24+6=30'], source_quotes: ['把伞递给了同伴'] })
  })

  it.each(['zero', 'other_dimension', 'no_passage'])('does not invent required source quotes for school open %s', async scenario => {
    const f = fixture()
    const id = scenario === 'other_dimension' ? 'answer' : 'evidence'
    const name = scenario === 'other_dimension' ? '结果' : '文本依据'
    f.change({ profile: 'school_open_scientific_explanation_v1', material: { status: 'bound', stimulus_text: scenario === 'no_passage' ? '' : '甲的电阻为十欧姆。' },
      dimensions: [{ ...f.get().dimensions[0], dimension_id: id }] })
    render(<LearningRubricReviewPanel child={child} request={f.request} />)
    await screen.findByLabelText(`${name}评分`); conditions()
    fireEvent.change(screen.getByLabelText(`${name}评分`), { target: { value: scenario === 'zero' ? '0' : '2' } })
    fireEvent.change(screen.getByLabelText(`${name}回答摘录`), { target: { value: '24+6=30' } })
    fireEvent.change(screen.getByLabelText(`${name}评分理由`), { target: { value: '根据原回答核对，不伪造材料依据。' } })
    expect(screen.getByRole('button', { name: '保存评分审核' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: '保存评分审核' }))
    await waitFor(() => expect(f.get().review_revision).toBe(1))
    expect(f.get().review.dimensions[0]).not.toHaveProperty('source_quotes')
  })

  it('adopts only an explicitly selected original material span and persists it without scoring or submitting', async () => {
    const f = fixture()
    const text = '甲的电阻为十欧姆，乙的电阻为二十欧姆。'
    f.change({ profile: 'school_open_scientific_explanation_v1', material: { status: 'bound', stimulus_text: text },
      dimensions: [{ ...f.get().dimensions[0], dimension_id: 'evidence' }] })
    const first = render(<LearningRubricReviewPanel child={child} request={f.request} />)
    const original = await screen.findByLabelText('材料原文') as HTMLTextAreaElement
    expect(original).toHaveAttribute('readonly')
    const adopt = screen.getByRole('button', { name: '引用选中材料' })
    expect(adopt).toBeDisabled()
    original.focus(); original.setSelectionRange(10, 19); fireEvent.select(original)
    expect(screen.getByLabelText('文本依据材料摘录')).toHaveValue('')
    fireEvent.click(adopt)
    expect(screen.getByLabelText('文本依据材料摘录')).toHaveValue(text.slice(10, 19))
    expect(screen.getByLabelText('文本依据评分')).toHaveValue('')
    expect(f.request.mock.calls.every(([, init]) => !init?.method)).toBe(true)
    first.unmount()
    render(<LearningRubricReviewPanel child={child} request={f.request} />)
    await screen.findByLabelText('材料原文')
    expect(screen.getByLabelText('文本依据材料摘录')).toHaveValue(text.slice(10, 19))
    expect(screen.getByRole('button', { name: '引用选中材料' })).toBeDisabled()
  })

  it('keeps another child isolated from delayed responses and pending writes', async () => {
    const f = fixture(true); const original = f.request.getMockImplementation()!; let finish!: (value: unknown) => void
    f.request.mockImplementation((path, init) => {
      if (path.includes('/child-b/')) return Promise.resolve({ ...local, child_id: 'child-b', items: [], total: 0, offset: 0 })
      return init?.method === 'POST' ? new Promise(resolve => { finish = resolve }) : original(path, init)
    })
    const view = render(<LearningRubricReviewPanel child={child} request={f.request} />)
    await screen.findByLabelText('目标1回答摘录'); conditions()
    fireEvent.click(screen.getByRole('button', { name: '保存评分审核' }))
    view.rerender(<LearningRubricReviewPanel child={{ ...child, child_id: 'child-b', nickname: '孩子乙' }} request={f.request} />)
    await screen.findByText('暂无待审核回答')
    await act(async () => finish({ ...local, assessment: f.get(), event: { assessment_id: 'assessment-a' } }))
    expect(screen.queryByText('先算24+6=30，再算30*3=90。')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '重试原审核' })).not.toBeInTheDocument()
    expect(f.request.mock.calls.filter(([path, init]) => path.includes('/child-b/') && init?.method === 'POST')).toHaveLength(0)
  })

  it('rejects response ownership mismatches instead of rendering another child answer', async () => {
    const f = fixture(true); f.change({ child_id: 'child-b' })
    render(<LearningRubricReviewPanel child={child} request={f.request} />)
    await screen.findByRole('alert')
    expect(screen.queryByText(f.get().response)).not.toBeInTheDocument()
  })

  it('blocks approval on changed sources without removing the history', async () => {
    const f = fixture(true); f.change({ source_status: 'changed', score_status: 'source_changed' })
    render(<LearningRubricReviewPanel child={child} request={f.request} />)
    await screen.findByText('原题或回答已变化')
    expect(screen.getByRole('button', { name: '保存评分审核' })).toBeDisabled()
    expect(screen.getByText('持久审核记录')).toBeInTheDocument()
  })

  it('does not approve unseen image material or fetch an arbitrary asset URL', async () => {
    const f = fixture(true); f.change({ material: { status: 'bound', stimulus_asset: 'https://untrusted.invalid/private-image' } })
    render(<LearningRubricReviewPanel child={child} request={f.request} />)
    await screen.findByLabelText('目标1回答摘录'); conditions()
    expect(screen.getByRole('button', { name: '保存评分审核' })).toBeDisabled()
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(f.request.mock.calls.every(([path]) => path.startsWith('/v1/parent/children/child-a/learning-rubrics'))).toBe(true)
  })

  it('does not transmit on storage failure and prevents duplicate writes', async () => {
    const f = fixture(true)
    render(<LearningRubricReviewPanel child={child} request={f.request} />)
    await screen.findByLabelText('目标1回答摘录'); conditions()
    const button = screen.getByRole('button', { name: '保存评分审核' })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota') })
    fireEvent.click(button); fireEvent.click(button)
    await screen.findByText('无法保存本机审核草稿，新审核尚未发送。')
    expect(f.request.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(0)
  })

  it('replays an old accepted key without overwriting a newer withdrawal projection', async () => {
    const f = fixture(true); const original = f.request.getMockImplementation()!
    let accepted: any; let first = true
    f.request.mockImplementation(async (path, init) => {
      if (!init?.method) return original(path, init)
      if (first) {
        first = false; accepted = await original(path, init)
        const withdrawal = { ...accepted.event, operation: 'retract', review_revision: 2, reason: '随后撤回', score_status: 'withdrawn' }
        accepted.history.push(withdrawal)
        f.change({ review_revision: 2, score_status: 'withdrawn', review: withdrawal })
        throw new ApiError('回执丢失')
      }
      const current: any = await original(path)
      return { ...current, event: accepted.event }
    })
    const view = render(<LearningRubricReviewPanel child={child} request={f.request} />)
    await screen.findByLabelText('目标1回答摘录'); conditions()
    fireEvent.click(screen.getByRole('button', { name: '保存评分审核' }))
    await screen.findByRole('button', { name: '重试原审核' })
    const firstBody = f.request.mock.calls.find(([, init]) => init?.method === 'POST')![1]!.body
    view.unmount(); render(<LearningRubricReviewPanel child={child} request={f.request} />)
    fireEvent.click(await screen.findByRole('button', { name: '重试原审核' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: '重试原审核' })).not.toBeInTheDocument())
    expect(screen.getByText('评分审核已撤回')).toBeInTheDocument()
    expect(screen.getByText('这份评分当前不能用于独立掌握门槛。')).toBeInTheDocument()
    expect(f.request.mock.calls.filter(([, init]) => init?.method === 'POST')[1][1]!.body).toBe(firstBody)
  })

  it('keeps the pending key when the nested qualification or old receipt cannot be verified', async () => {
    const f = fixture(true); const original = f.request.getMockImplementation()!
    f.request.mockImplementation(async (path, init) => {
      const response: any = await original(path, init)
      if (init?.method === 'POST') response.qualification = { ...response.qualification, assessment_id: 'another-answer' }
      return response
    })
    render(<LearningRubricReviewPanel child={child} request={f.request} />)
    await screen.findByLabelText('目标1回答摘录'); conditions()
    fireEvent.click(screen.getByRole('button', { name: '保存评分审核' }))
    expect(await screen.findByRole('button', { name: '重试原审核' })).toBeInTheDocument()
    expect(f.request.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
  })

  it('does not merge a newer qualification receipt with an older readonly assessment', async () => {
    const f = fixture(true); const original = f.request.getMockImplementation()!
    f.request.mockImplementation(async (path, init) => {
      const response: any = await original(path, init)
      if (!path.includes('?')) response.qualification = { ...response.qualification, eligible_for_mastery_gate: true, receipt: { assessment_id: 'assessment-a', review_revision: 3 } }
      return response
    })
    render(<LearningRubricReviewPanel child={child} request={f.request} />)
    await screen.findByRole('alert')
    expect(screen.queryByText('这份评分符合进入掌握门槛的条件。')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '保存评分审核' })).not.toBeInTheDocument()
  })
})


it('shows a true exam-slot origin with no learning objective and hides internal identities', async () => {
  const f = fixture()
  f.change({ source_kind: 'exam_open_review', objective_id: null,
    exam_slot: { run_id: 'private-run-id', position: 3, item_id: 'private-item-id', source_version: 'source-v1' } })
  render(<LearningRubricReviewPanel child={child} request={f.request} />)
  expect(await screen.findByText('整卷第3题原作答审核。评分会更新本卷参考结果，后续仍需独立复测。')).toBeInTheDocument()
  expect(screen.getByText('题目任务点依据')).toBeInTheDocument()
  expect(screen.queryByText('目标对应依据')).not.toBeInTheDocument()
  expect(screen.queryByText('private-run-id')).not.toBeInTheDocument()
  expect(screen.getByText('先算24+6=30，再算30*3=90。')).toBeInTheDocument()
})
