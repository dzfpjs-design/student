import {flushSync} from 'react-dom'
import {Activity,StrictMode} from 'react'
import {ParentPaperFormalQueue} from './ParentPaperFormalQueue'
import {changeParentDraftAuthority} from './parentDraftAuthority'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './api'
import { CurriculumSupplyReviewPanel } from './CurriculumSupplyReviewPanel'
import { clearCurriculumSupplyLocalState, curriculumLocalKey, writingAuthorizationLocalKey } from './curriculumSupplyApi'
import type { CurriculumAudit, CurriculumOptions, CurriculumPackage, CurriculumParentRequest, CurriculumSourceOptions } from './curriculumSupplyTypes'

const child = { child_id: 'child-a', nickname: '孩子甲', grade: '四年级' }
const local = { local_only: true as const, cloud_transmission_allowed: false as const }
function fixture() {
  const options: CurriculumOptions = { ...local, child_id: child.child_id, grade: child.grade, semesters: ['上学期', '下学期'],
    sources: [{ source_id: 'book-a', title: '已审核四年级教材', subject: '数学', edition: '苏教版', available: true, blockers: [], excerpt_count: 1 }] }
  const details: CurriculumSourceOptions = { ...local, child_id: child.child_id, source_id: 'book-a',
    excerpts: [{ excerpt_id: 'excerpt-a', sequence: 1, text: '计算：125*(32+8)。' }],
    standards: [{ requirement_id: 'requirement-a', title: '运算律', stage_scope: '第二学段', requirement_text: '理解运算律并合理选择运算方法。',
      source_title: '数学课程标准', page_number: 12, quote: '理解运算律并合理选择运算方法。', available: true, blockers: [] }],
    seeds: [{ seed_item_id: 'seed-a', title: '四则运算', prompt: '计算原型', validator: 'arithmetic_expression_v1', stages: ['parallel'] }] }
  const scope = { grade: child.grade, subject: '数学', edition: '苏教版', semester: '上学期', standard_scope: '第二学段', textbook_source_id: 'book-a' }
  let item: CurriculumPackage = { ...local, child_id: child.child_id, candidate_id: 'candidate-a', state: 'draft', version: 1, eligible: false, blockers: [], next_action: 'independent_validate',
    supersedes: null, diagnostic_item_id: null, payload: { seed_item_id: 'seed-a', stage: 'parallel', excerpt_id: 'excerpt-a', requirement_id: 'requirement-a', standard_page: 12,
      standard_quote: details.standards[0].quote, validator: 'arithmetic_expression_v1', explanation: '先算括号，再相乘。', difficulty: 1, prompt: details.excerpts[0].text, expected_answer: null },
    review_packet: { fingerprint: 'fingerprint', scope, textbook: { source_id: 'book-a', title: options.sources[0].title, source_ref: 'private-local-source', version_label: '苏教版', review_status: 'approved', excerpt_id: 'excerpt-a', content_text: details.excerpts[0].text },
      standard: { source_id: 'standard-a', official_url: 'https://example.invalid/standard', page_number: 12, requirement_text: details.standards[0].requirement_text, quote: details.standards[0].quote }, review_scope: 'question_only' }, events: [] }
  let audit: CurriculumAudit = { ...local, child_id: child.child_id, current_curriculum: [{ version: 1, scope }], blocked_job_counts: { LOCAL_TEMPLATE_UNAVAILABLE: 2 },
    eligible_curriculum_items: 0, qualified_stages: [], packages: [{ ...item, prompt: item.payload.prompt }], thinking_or_transfer_coverage_claim_allowed: false }
  const adapter = vi.fn<CurriculumParentRequest>(async (path, init) => {
    if (!init?.method) {
      if (path.endsWith('/sources/book-a/options')) return details
      if (path.endsWith('/options')) return options
      if (path.endsWith('/audit')) return audit
      return item
    }
    const body = JSON.parse(String(init.body))
    if (path.endsWith('/select')) { audit = { ...audit, current_curriculum: [{ version: 2, scope: body.scope }] }; return { ...local, version: 2, scope: body.scope } }
    if (!path.endsWith('/generate') && body.expected_version !== item.version) throw new ApiError('冲突', 409, 'CURRICULUM_VERSION_CONFLICT')
    if (path.endsWith('/generate')) item = { ...item, candidate_id: 'candidate-b', version: 1, state: 'draft', eligible: false, payload: { ...item.payload, ...body.source } }
    else {
      const kind = path.split('/').at(-1)!
      const transitions = { validate: 'validated', review: 'reviewed', publish: 'published', withdraw: 'withdrawn' } as const
      const state = transitions[kind as keyof typeof transitions]
      item = { ...item, state, version: item.version + 1, eligible: state === 'published', events: [...item.events, {
        version: item.version + 1, kind, actor: 'parent', created_at: '2026-09-07T10:00:00Z', result: { state, blockers: [], note: body.note,
          ...(kind === 'validate' ? { check: { validator: 'arithmetic_expression_v1', computed_answer: '5000', question_hash: 'hash', payload_hash: 'hash' } } : {}) }
      }] }
    }
    audit = { ...audit, eligible_curriculum_items: item.eligible ? 1 : 0, packages: [{ ...item, prompt: item.payload.prompt }] }
    return item
  })
  return { adapter, options, details, item, setItem: (next: CurriculumPackage) => { item = next } }
}

describe('CurriculumSupplyReviewPanel', () => {
  afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks() })

  it.each(['draft', 'validated', 'reviewed'] as const)('blocks %s progression when bound material is no longer readable', async state => {
    const f = fixture()
    const original = JSON.stringify({ schema: 'reviewed_open_response_source_v2', context_requirement: 'external_material',
      material_semantics: 'text_only', prompt: '根据给定条件解释结论。', task_points: ['使用完整给定条件'],
      material_bindings: [{ material_key: 'given' }] })
    f.setItem({ ...f.item, state, payload: { ...f.item.payload, seed_item_id: 'seed-a', stage: 'parallel', validator: 'reviewed_open_response_rubric_source_v1' },
      review_packet: { ...f.item.review_packet!, textbook: { ...f.item.review_packet!.textbook, content_text: original },
        current_materials: { status: 'blocked', reason: 'OPEN_RESPONSE_MATERIAL_SOURCE_NOT_REVIEWED', materials: [],
          local_only: true, cloud_transmission_allowed: false, mastery_claim_allowed: false } } })
    render(<CurriculumSupplyReviewPanel child={child} request={f.adapter} />)
    await screen.findByText('绑定原材料暂时无法核对或已经变化。请刷新并核对当前来源；不能继续审核发布。')
    fireEvent.change(screen.getByLabelText('审核备注'), { target: { value: '原备注仍保留，等待当前来源恢复。' } })
    const name = state === 'draft' ? '独立校验' : state === 'validated' ? '审核通过' : '发布到题库'
    expect(screen.getByRole('button', { name })).toBeDisabled()
    expect(screen.getByRole('button', { name: '撤回题目' })).toBeEnabled()
    expect(document.querySelector('.curriculum-supply-review')?.textContent).not.toContain('material_bindings')
  })

  it.each(['closed', 'open'])('uses the selected question type when one seed supports both: %s', async kind => {
    const f = fixture()
    const closedValidator = 'curriculum_objective_exact_source_v1'
    const openValidator = 'reviewed_open_response_rubric_source_v1'
    f.details.excerpts = [
      { excerpt_id: 'closed', sequence: 1, text: JSON.stringify({ schema: 'curriculum_objective_source_v1',
        prompt: '4盒彩笔每盒12支，共多少支？', expected_answer: { exact_answers: ['48'] } }) },
      { excerpt_id: 'open', sequence: 2, text: JSON.stringify({ schema: 'reviewed_open_response_source_v1',
        prompt: '解释4×12为什么表示彩笔总数。', task_points: ['说明盒数与每盒支数的关系'] }) },
    ]
    f.details.seeds = [openValidator, closedValidator].map(validator => ({ seed_item_id: 'seed-a',
      title: '乘法含义', prompt: '原有已审核题目', validator, stages: ['parallel'] }))
    render(<CurriculumSupplyReviewPanel child={child} request={f.adapter} />)
    await screen.findByRole('option', { name: /已审核四年级教材/ })
    fireEvent.change(screen.getByLabelText('当前教材'), { target: { value: 'book-a' } })
    await screen.findByRole('option', { name: /运算律/ })
    fireEvent.change(screen.getByLabelText('对照课标'), { target: { value: 'requirement-a:12' } })
    fireEvent.change(screen.getByLabelText('教材题目'), { target: { value: kind } })
    const select = screen.getByLabelText('题型依据') as HTMLSelectElement
    expect(select.options.length).toBe(2)
    fireEvent.change(select, { target: { value: 'seed-a' } })
    fireEvent.change(screen.getByLabelText('题目难度（1至5）'), { target: { value: '1' } })
    fireEvent.change(screen.getByLabelText('解题说明'), { target: { value: '按当前原题核对完整参考依据。' } })
    fireEvent.click(screen.getByRole('button', { name: '提取审核草案' }))
    await waitFor(() => expect(f.adapter.mock.calls.some(([path, init]) => path.endsWith('/generate') && init?.method === 'POST')).toBe(true))
    const call = f.adapter.mock.calls.find(([path, init]) => path.endsWith('/generate') && init?.method === 'POST')!
    const body = JSON.parse(String(call[1]!.body))
    expect(body.source.validator).toBe(kind === 'closed' ? closedValidator : openValidator)
    expect(body.source.excerpt_id).toBe(kind)
    // Changing question type requires a new explicit seed selection, including
    // after the old selection was saved locally; no hidden validator reuse.
    fireEvent.change(screen.getByLabelText('教材题目'), { target: { value: kind === 'closed' ? 'open' : 'closed' } })
    expect(screen.getByLabelText('题型依据')).toHaveValue('')
    expect(screen.getByRole('button', { name: '提取审核草案' })).toBeDisabled()
  })

  it('selects the school original listening validator from its scoped source schema', async () => {
    const f = fixture()
    f.details.excerpts = [{ excerpt_id: 'school-listening', sequence: 1, text: JSON.stringify({
      schema: 'school_listening_source_v1', subject: '英语', grade: '初三', knowledge_node_id: 'node-j3-english-listening',
      prompt: 'Where should Mira wait?', stimulus_text: 'Mira should wait at the red bridge.',
    }) }]
    f.details.seeds = [{ seed_item_id: 'school-listening-seed', title: '校内听力原题', prompt: 'Where should they wait?',
      validator: 'pet_listening_exact_source_v1', stages: ['parallel'] }]
    render(<CurriculumSupplyReviewPanel child={child} request={f.adapter} />)
    await screen.findByRole('option', { name: /已审核四年级教材/ })
    fireEvent.change(screen.getByLabelText('当前教材'), { target: { value: 'book-a' } })
    await screen.findByRole('option', { name: /运算律/ })
    fireEvent.change(screen.getByLabelText('对照课标'), { target: { value: 'requirement-a:12' } })
    fireEvent.change(screen.getByLabelText('教材题目'), { target: { value: 'school-listening' } })
    fireEvent.change(screen.getByLabelText('题型依据'), { target: { value: 'school-listening-seed' } })
    fireEvent.change(screen.getByLabelText('题目难度（1至5）'), { target: { value: '1' } })
    fireEvent.change(screen.getByLabelText('解题说明'), { target: { value: '按原音频核对明确地点。' } })
    fireEvent.click(screen.getByRole('button', { name: '提取审核草案' }))
    await waitFor(() => expect(f.adapter.mock.calls.some(([path, init]) => path.endsWith('/generate') && init?.method === 'POST')).toBe(true))
    const call = f.adapter.mock.calls.find(([path, init]) => path.endsWith('/generate') && init?.method === 'POST')!
    expect(JSON.parse(String(call[1]!.body)).source).toMatchObject({
      excerpt_id: 'school-listening', seed_item_id: 'school-listening-seed', validator: 'pet_listening_exact_source_v1',
    })
  })

  it('re-reads mounted curriculum after an all-child restore clear and does not recreate old draft state', async () => {
    const f = fixture()
    render(<CurriculumSupplyReviewPanel child={child} request={f.adapter} />)
    await screen.findByLabelText('审核备注')
    fireEvent.change(screen.getByLabelText('审核备注'), { target: { value: '恢复前草稿' } })
    const reads = f.adapter.mock.calls.length
    act(() => clearCurriculumSupplyLocalState())
    await waitFor(() => expect(screen.getByLabelText('审核备注')).toHaveValue(''))
    expect(f.adapter.mock.calls.length).toBeGreaterThan(reads)
    expect(f.adapter.mock.calls.every(([, init]) => !init?.method)).toBe(true)
  })

  it.each(['draft', 'math', 'missing_id', 'published', 'open', 'material'])('mounts purpose authorization only for a published supported diagnostic item: %s', async mode => {
    const f = fixture()
    f.setItem({ ...f.item, state: mode === 'draft' ? 'draft' : 'published', eligible: mode !== 'draft',
      diagnostic_item_id: mode === 'missing_id' ? null : 'writing-a',
      payload: { ...f.item.payload, seed_item_id: 'seed-a', validator: mode === 'math' ? 'arithmetic_expression_v1' : ['open', 'material'].includes(mode) ? 'reviewed_open_response_rubric_source_v1' : 'pet_writing_rubric_source_v1' } })
    const request = vi.fn<CurriculumParentRequest>((path, init) => path.includes('/writing-question-authorizations/') ? Promise.resolve({
      local_only: true, cloud_transmission_allowed: false, child_id: child.child_id, diagnostic_item_id: 'writing-a',
      status: 'authorization_required', revision: 0, reason: null, authorization_id: null, item_version: 'i1', source_version: 's1', curriculum_version: 'c1',
      purpose_version: mode === 'material' ? 'writing_question_material_formative_text_v2' : 'writing_question_formative_text_v1',
      preview: { question: 'Write an email about your trip.', task_points: ['Describe your trip.'],
        ...(mode === 'material' ? { materials: [{ material_key: 'm1', text: '甲地节水量记录', title: '用水记录', license_status: 'public', source_version: 'ms1', text_hash: 'mh1' }] } : {}) },
      rights_review_authority: 'parent_declaration_not_legal_or_network_verification',
      license_options: ['CC0-1.0', 'original_author', 'explicit_cloud_processing_permission']
    }) : f.adapter(path, init))
    render(<CurriculumSupplyReviewPanel child={child} request={request} />)
    await screen.findByLabelText('审核备注')
    if (['published', 'open', 'material'].includes(mode)) {
      await screen.findByText('尚未授权此题用途')
      expect(screen.getByText('Write an email about your trip.')).toBeInTheDocument()
      expect(request.mock.calls.filter(([path]) => path.includes('/writing-question-authorizations/'))).toEqual([
        ['/v1/parent/children/child-a/writing-question-authorizations/writing-a', { cache: 'no-store' }]
      ])
      if (mode === 'material') {
        expect(screen.getByText('甲地节水量记录')).toBeInTheDocument()
        expect(screen.getByRole('group', { name: '材料1：用水记录' })).toBeInTheDocument()
      }
      fireEvent.change(screen.getAllByLabelText('权利依据')[0], { target: { value: 'original_author' } })
      expect(localStorage.getItem(writingAuthorizationLocalKey(child.child_id, 'writing-a'))).not.toBeNull()
      localStorage.setItem(writingAuthorizationLocalKey('child-b', 'writing-b'), 'other child draft')
      fireEvent.change(screen.getByLabelText('审核备注'), { target: { value: '撤回当前原题' } })
      fireEvent.click(screen.getByRole('button', { name: '撤回题目' }))
      await waitFor(() => expect(screen.queryByRole('region', { name: '孩子甲的题目文字用途授权' })).not.toBeInTheDocument())
      expect(localStorage.getItem(writingAuthorizationLocalKey(child.child_id, 'writing-a'))).toBeNull()
      expect(localStorage.getItem(writingAuthorizationLocalKey('child-b', 'writing-b'))).toBe('other child draft')
    } else {
      expect(screen.queryByRole('region', { name: '孩子甲的题目文字用途授权' })).not.toBeInTheDocument()
      expect(request.mock.calls.some(([path]) => path.includes('/writing-question-authorizations/'))).toBe(false)
    }
  })

  it('persists an explicit source-stage draft and submits the same original binding after restart', async () => {
    const f = fixture()
    const context = { ...local, schema: 'specialized_stage_review_v1' as const, child_id: child.child_id,
      candidate_id: f.item.candidate_id, binding_version: 'source-stage-v1', source_root_item_id: 'seed-a', knowledge_node_id: 'node-a',
      source_prompt: '原题：计算(24+6)乘3。', candidate_prompt: f.item.payload.prompt, candidate_explanation: f.item.payload.explanation,
      status: 'needs_review', allowed_stages: ['near_transfer', 'far_transfer'] as const,
      qualification_scope: 'explicit_human_source_stage_review_not_child_mastery' as const }
    f.setItem({ ...f.item, state: 'validated', version: 2, stage_review_context: { ...context, allowed_stages: [...context.allowed_stages] } })
    const first = render(<CurriculumSupplyReviewPanel child={child} request={f.adapter} />)
    await screen.findByLabelText('题目迁移阶段')
    fireEvent.change(screen.getByLabelText('审核备注'), { target: { value: '已核对原题与新题的不同情境' } })
    fireEvent.change(screen.getByLabelText('题目迁移阶段'), { target: { value: 'far_transfer' } })
    expect(screen.getByRole('button', { name: '审核通过' })).toBeDisabled()
    for (const [label, value] of [['原题依据摘录', context.source_prompt], ['新题依据摘录', context.candidate_prompt],
      ['相同技能依据', '两题都要求核对运算顺序与运算律的使用条件。'], ['情境变化依据', '已逐项对照两题条件，人工确认所标注的迁移范围。']]) {
      fireEvent.change(screen.getByLabelText(label), { target: { value } })
    }
    first.unmount()
    render(<CurriculumSupplyReviewPanel child={child} request={f.adapter} />)
    await screen.findByLabelText('题目迁移阶段')
    expect(screen.getByLabelText('题目迁移阶段')).toHaveValue('far_transfer')
    expect(screen.getByLabelText('原题依据摘录')).toHaveValue(context.source_prompt)
    fireEvent.click(screen.getByRole('button', { name: '审核通过' }))
    await waitFor(() => expect(f.adapter.mock.calls.some(([path, init]) => path.endsWith('/review') && !!init?.body)).toBe(true))
    const request = f.adapter.mock.calls.find(([path, init]) => path.endsWith('/review') && init?.body)!
    expect(JSON.parse(String(request[1]!.body)).stage_review).toMatchObject({ schema: context.schema,
      stage: 'far_transfer', binding_version: context.binding_version, source_quote: context.source_prompt, candidate_quote: context.candidate_prompt })
  })

  it('focuses the real textbook selection only after options load and never GETs a package', async () => {
    const f = fixture(); const original = f.adapter.getMockImplementation()!
    let release!: (value: unknown) => void
    const options = new Promise(resolve => { release = resolve })
    f.adapter.mockImplementation((path, init) => path.endsWith('/options') ? options : original(path, init))
    const done = vi.fn()
    render(<CurriculumSupplyReviewPanel child={child} request={f.adapter} focusRequest={{ objectId: `selection:${child.child_id}`, nonce: 'selection' }} onFocusResolved={done} />)
    expect(done).not.toHaveBeenCalled()
    await act(async () => release(f.options))
    await waitFor(() => expect(done).toHaveBeenCalledWith('selection'))
    expect(document.activeElement).toHaveAttribute('data-work-object', `selection:${child.child_id}`)
    expect(f.adapter.mock.calls.every(([path, init]) => !init?.method && /\/(options|audit)$/.test(path))).toBe(true)
    expect(screen.getByLabelText('当前教材')).toBeEnabled()
  })

  it('does not acknowledge selection success when parent options authorization fails', async () => {
    const f = fixture(); f.adapter.mockRejectedValue(new ApiError('未授权', 403))
    const done = vi.fn()
    render(<CurriculumSupplyReviewPanel child={child} request={f.adapter} focusRequest={{ objectId: `selection:${child.child_id}`, nonce: 'selection' }} onFocusResolved={done} />)
    await waitFor(() => expect(done).toHaveBeenCalledWith('selection', expect.any(String)))
    expect(done).toHaveBeenCalledTimes(1)
    expect(f.adapter.mock.calls).toHaveLength(1)
  })

  it('loads an exact off-queue package while preserving per-package notes and the generation draft across restart', async () => {
    const f = fixture()
    const other = { ...f.item, candidate_id: 'candidate-b', payload: { ...f.item.payload, prompt: '另一道原题' } }
    const request = vi.fn<CurriculumParentRequest>((path, init) => path.endsWith('/candidate-b') ? Promise.resolve(other) : f.adapter(path, init))
    const done = vi.fn()
    const view = render(<CurriculumSupplyReviewPanel child={child} request={request} />)
    await screen.findByLabelText('审核备注')
    fireEvent.change(screen.getByLabelText('审核备注'), { target: { value: '甲原题未提交备注' } })
    fireEvent.change(screen.getByLabelText('解题说明'), { target: { value: '未提交制作草稿' } })
    view.rerender(<CurriculumSupplyReviewPanel child={child} request={request} focusRequest={{ objectId: 'candidate-b', nonce: 'b' }} onFocusResolved={done} />)
    await waitFor(() => expect(done).toHaveBeenCalledWith('b'))
    expect(document.activeElement).toHaveAttribute('data-work-object', 'candidate-b')
    expect(screen.getByLabelText('审核备注')).toHaveValue('')
    fireEvent.change(screen.getByLabelText('审核备注'), { target: { value: '乙原题未提交备注' } })
    view.unmount()
    render(<CurriculumSupplyReviewPanel child={child} request={request} focusRequest={{ objectId: 'candidate-a', nonce: 'a' }} onFocusResolved={done} />)
    await waitFor(() => expect(done).toHaveBeenCalledWith('a'))
    expect(screen.getByLabelText('审核备注')).toHaveValue('甲原题未提交备注')
    expect(screen.getByLabelText('解题说明')).toHaveValue('未提交制作草稿')
    expect(JSON.parse(localStorage.getItem(curriculumLocalKey(child.child_id))!).notesByCandidate['candidate-b']).toBe('乙原题未提交备注')
    expect(request.mock.calls.every(([, init]) => !init?.method)).toBe(true)
  })

  it.each(['forbidden', 'missing', 'wrong-child', 'withdrawn'])('fails exact package focus %s without selecting the first package', async failure => {
    const f = fixture()
    const request = vi.fn<CurriculumParentRequest>((path, init) => path.endsWith('/candidate-b')
      ? failure === 'forbidden' || failure === 'missing' ? Promise.reject(new ApiError('不可读取', failure === 'forbidden' ? 403 : 404))
        : Promise.resolve({ ...f.item, candidate_id: 'candidate-b', ...(failure === 'wrong-child' ? { child_id: 'child-b' } : {}), ...(failure === 'withdrawn' ? { state: 'withdrawn' } : {}) })
      : f.adapter(path, init))
    const done = vi.fn()
    render(<CurriculumSupplyReviewPanel child={child} request={request} focusRequest={{ objectId: 'candidate-b', nonce: failure }} onFocusResolved={done} />)
    await waitFor(() => expect(done).toHaveBeenCalledWith(failure, expect.any(String)))
    expect(done).toHaveBeenCalledTimes(1)
    expect(document.querySelector('[data-work-object="candidate-a"]')).toBeNull()
    expect(request.mock.calls.some(([path]) => path.endsWith('/candidate-a'))).toBe(false)
  })

  it('keeps the current package when an earlier deep-link response arrives late', async () => {
    const f = fixture()
    let release!: (value: unknown) => void
    const delayed = new Promise(resolve => { release = resolve })
    const request = vi.fn<CurriculumParentRequest>((path, init) => path.endsWith('/candidate-b') ? delayed
      : path.endsWith('/candidate-c') ? Promise.resolve({ ...f.item, candidate_id: 'candidate-c' }) : f.adapter(path, init))
    const done = vi.fn()
    const view = render(<CurriculumSupplyReviewPanel child={child} request={request} focusRequest={{ objectId: 'candidate-b', nonce: 'old' }} onFocusResolved={done} />)
    await waitFor(() => expect(request.mock.calls.some(([path]) => path.endsWith('/candidate-b'))).toBe(true))
    view.rerender(<CurriculumSupplyReviewPanel child={child} request={request} focusRequest={{ objectId: 'candidate-c', nonce: 'current' }} onFocusResolved={done} />)
    await waitFor(() => expect(done).toHaveBeenCalledWith('current'))
    await act(async () => release({ ...f.item, candidate_id: 'candidate-b' }))
    expect(done).toHaveBeenCalledTimes(1)
    expect(document.activeElement).toHaveAttribute('data-work-object', 'candidate-c')
    expect(document.querySelector('[data-work-object="candidate-b"]')).toBeNull()
  })

  it('keeps an unresolved mutation bound to the original package while reading a different one', async () => {
    const f = fixture()
    const request = vi.fn<CurriculumParentRequest>((path, init) => init?.method ? Promise.reject(new ApiError('回执未知'))
      : path.endsWith('/candidate-b') ? Promise.resolve({ ...f.item, candidate_id: 'candidate-b' }) : f.adapter(path, init))
    const done = vi.fn()
    const view = render(<CurriculumSupplyReviewPanel child={child} request={request} />)
    await screen.findByLabelText('审核备注')
    fireEvent.change(screen.getByLabelText('审核备注'), { target: { value: '原草案备注' } })
    fireEvent.click(screen.getByRole('button', { name: '独立校验' }))
    await screen.findByRole('alert')
    const pending = JSON.parse(localStorage.getItem(curriculumLocalKey(child.child_id))!).pending
    view.rerender(<CurriculumSupplyReviewPanel child={child} request={request} focusRequest={{ objectId: 'candidate-b', nonce: 'b' }} onFocusResolved={done} />)
    await waitFor(() => expect(done).toHaveBeenCalledWith('b'))
    const saved = JSON.parse(localStorage.getItem(curriculumLocalKey(child.child_id))!)
    expect(saved.pending).toEqual(pending)
    expect(saved.notesByCandidate['candidate-a']).toBe('原草案备注')
    expect(request.mock.calls.filter(([, init]) => init?.method)).toHaveLength(1)
    expect(screen.getByRole('button', { name: '独立校验' })).toBeDisabled()
  })

  it.each(['pet_listening_exact_source_v1', 'curriculum_objective_exact_source_v1'])('labels %s references without claiming arithmetic recomputation', async (validator) => {
    const f = fixture()
    f.setItem({ ...f.item, events: [{ version: 2, kind: 'validate', actor: 'parent', created_at: '2026-09-07T10:00:00Z', result: { state: 'validated', blockers: [], check: { validator, computed_answer: 'at the red bridge', question_hash: 'h', payload_hash: 'h' } } }] })
    render(<CurriculumSupplyReviewPanel child={child} request={f.adapter} />)
    expect(await screen.findByText('审核原始参考答案')).toBeInTheDocument()
    expect(screen.queryByText('独立复算结果')).not.toBeInTheDocument()
    expect(screen.queryByText(/基础四则运算规则复算/)).not.toBeInTheDocument()
  })

  it('keeps evidence beside decisions and persists the review/publish/withdraw journey across remount', async () => {
    const f = fixture()
    const view = render(<CurriculumSupplyReviewPanel child={child} request={f.adapter} />)
    expect(await screen.findByRole('button', { name: '独立校验' })).toBeEnabled()
    expect(screen.getByText('缺少该知识点的题目模板：2项')).toBeInTheDocument()
    expect(screen.queryByText('candidate-a')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '独立校验' }))
    expect(await screen.findByText('5000')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('审核备注'), { target: { value: '原题与课标已核对' } })
    fireEvent.click(screen.getByRole('button', { name: '审核通过' }))
    expect(await screen.findByRole('button', { name: '发布到题库' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: '发布到题库' }))
    expect(await screen.findByText('当前有效题目：1道')).toBeInTheDocument()
    view.unmount()
    render(<CurriculumSupplyReviewPanel child={child} request={f.adapter} />)
    expect(await screen.findByText('当前有效题目：1道')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('审核备注'), { target: { value: '题源需要修订' } })
    fireEvent.click(screen.getByRole('button', { name: '撤回题目' }))
    expect(await screen.findByText('当前有效题目：0道')).toBeInTheDocument()
    expect(f.adapter.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(4)
  })

  it('uses real source options for selecting curriculum and generating a draft without ID entry', async () => {
    const f = fixture()
    render(<CurriculumSupplyReviewPanel child={child} request={f.adapter} />)
    await screen.findByRole('option', { name: /已审核四年级教材/ })
    fireEvent.change(screen.getByLabelText('当前教材'), { target: { value: 'book-a' } })
    await screen.findByRole('option', { name: /运算律/ })
    fireEvent.change(screen.getByLabelText('对照课标'), { target: { value: 'requirement-a:12' } })
    fireEvent.click(screen.getByRole('button', { name: '保存当前教材' }))
    await waitFor(() => expect(f.adapter.mock.calls.some(([path, init]) => path.endsWith('/select') && init?.method === 'POST')).toBe(true))
    fireEvent.change(screen.getByLabelText('教材题目'), { target: { value: 'excerpt-a' } })
    fireEvent.change(screen.getByLabelText('题型依据'), { target: { value: 'seed-a' } })
    fireEvent.change(screen.getByLabelText('题目难度（1至5）'), { target: { value: '2' } })
    fireEvent.change(screen.getByLabelText('解题说明'), { target: { value: '先算括号内，再乘125。' } })
    fireEvent.change(screen.getByLabelText('审核备注'), { target: { value: '上一道题的备注' } })
    fireEvent.click(screen.getByRole('button', { name: '提取审核草案' }))
    await waitFor(() => expect(f.adapter.mock.calls.some(([path, init]) => path.endsWith('/generate') && init?.method === 'POST')).toBe(true))
    const body = JSON.parse(String(f.adapter.mock.calls.find(([path, init]) => path.endsWith('/generate') && init?.method === 'POST')![1]!.body))
    expect(body.expected_scope_version).toBe(2)
    expect(body.source.excerpt_id).toBe('excerpt-a')
    expect(body.source.difficulty).toBe(2)
    expect(body.actor).toBeUndefined()
    await waitFor(() => expect(screen.getByLabelText('审核备注')).toHaveValue(''))
  })

  it('retains unknown submissions across restart and retries the same action key', async () => {
    const f = fixture()
    const original = f.adapter.getMockImplementation()!
    let fail = true
    f.adapter.mockImplementation(async (path, init) => { if (init?.method === 'POST' && fail) throw new ApiError('断线', undefined); return original(path, init) })
    const view = render(<CurriculumSupplyReviewPanel child={child} request={f.adapter} />)
    fireEvent.click(await screen.findByRole('button', { name: '独立校验' }))
    await screen.findByRole('button', { name: '重试原请求' })
    const first = f.adapter.mock.calls.find(([, init]) => init?.method === 'POST')![1]!.body
    view.unmount(); fail = false
    render(<CurriculumSupplyReviewPanel child={child} request={f.adapter} />)
    fireEvent.click(await screen.findByRole('button', { name: '重试原请求' }))
    await screen.findByText('5000')
    const calls = f.adapter.mock.calls.filter(([, init]) => init?.method === 'POST')
    expect(calls[1][1]!.body).toBe(first)
  })

  it('refreshes conflicts but never silently approves a newer version', async () => {
    const f = fixture()
    render(<CurriculumSupplyReviewPanel child={child} request={f.adapter} />)
    const button = await screen.findByRole('button', { name: '独立校验' })
    f.setItem({ ...f.item, version: 2, state: 'validated' })
    fireEvent.click(button)
    expect(await screen.findByText(/版本已变化/)).toBeInTheDocument()
    expect(f.adapter.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
    expect(screen.getByRole('button', { name: '放弃旧请求' })).toBeInTheDocument()
  })

  it('shows missing material with a real host navigation action', async () => {
    const f = fixture(); f.options.sources = []
    const open = vi.fn()
    render(<CurriculumSupplyReviewPanel child={child} request={f.adapter} onOpenMaterials={open} />)
    fireEvent.click(await screen.findByRole('button', { name: '管理教材材料' }))
    expect(open).toHaveBeenCalledOnce()
    expect(screen.getByRole('button', { name: '保存当前教材' })).toBeDisabled()
  })

  it('keeps the pending decision on expired authorization without inventing a parent identity', async () => {
    const f = fixture(); const unlock = vi.fn()
    const original = f.adapter.getMockImplementation()!
    f.adapter.mockImplementation((path, init) => init?.method === 'POST' ? Promise.reject(new ApiError('失效', 401)) : original(path, init))
    render(<CurriculumSupplyReviewPanel child={child} request={f.adapter} onRequireParentAuth={unlock} />)
    fireEvent.click(await screen.findByRole('button', { name: '独立校验' }))
    fireEvent.click(await screen.findByRole('button', { name: '重新解锁家长操作' }))
    expect(unlock).toHaveBeenCalledOnce()
    expect(screen.getByRole('button', { name: '重试原请求' })).toBeEnabled()
    const init = f.adapter.mock.calls.find(([, init]) => init?.method === 'POST')![1]!
    expect(init.headers).toBeUndefined()
    expect(JSON.parse(String(init.body)).actor).toBeUndefined()
  })

  it('does not let a delayed first child response or pending request enter the second child view', async () => {
    const f = fixture()
    let finish!: (value: unknown) => void
    const original = f.adapter.getMockImplementation()!
    f.adapter.mockImplementation((path, init) => {
      if (path.includes('/child-b/')) {
        if (path.endsWith('/options')) return Promise.resolve({ ...f.options, child_id: 'child-b', sources: [] })
        return Promise.resolve({ ...local, child_id: 'child-b', current_curriculum: [], blocked_job_counts: {}, eligible_curriculum_items: 0, packages: [], qualified_stages: [], thinking_or_transfer_coverage_claim_allowed: false })
      }
      if (init?.method === 'POST') return new Promise(resolve => { finish = resolve })
      return original(path, init)
    })
    const view = render(<CurriculumSupplyReviewPanel child={child} request={f.adapter} />)
    fireEvent.click(await screen.findByRole('button', { name: '独立校验' }))
    view.rerender(<CurriculumSupplyReviewPanel child={{ ...child, child_id: 'child-b', nickname: '孩子乙' }} request={f.adapter} />)
    await screen.findByText('还没有审核草案。')
    finish({ ...f.item, state: 'validated', version: 2 })
    await waitFor(() => expect(screen.queryByText('题目与依据')).not.toBeInTheDocument())
    expect(screen.queryByRole('button', { name: '重试原请求' })).not.toBeInTheDocument()
    expect(f.adapter.mock.calls.filter(([path, init]) => path.includes('/child-b/') && init?.method === 'POST')).toHaveLength(0)
  })

  it('sends only one write on repeated clicks and blocks stale source approval', async () => {
    const f = fixture(); let finish!: (value: unknown) => void
    const original = f.adapter.getMockImplementation()!
    f.adapter.mockImplementation((path, init) => init?.method === 'POST' ? new Promise(resolve => { finish = resolve }) : original(path, init))
    render(<CurriculumSupplyReviewPanel child={child} request={f.adapter} />)
    const button = await screen.findByRole('button', { name: '独立校验' })
    fireEvent.click(button); fireEvent.click(button)
    expect(f.adapter.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
    f.setItem({ ...f.item, state: 'blocked', version: 2, blockers: ['CURRICULUM_SOURCE_CHANGED'], review_packet: null })
    finish({ ...f.item, state: 'blocked', version: 2, blockers: ['CURRICULUM_SOURCE_CHANGED'], review_packet: null })
    expect(await screen.findByText('审核后来源已修订，需要重新核对')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '独立校验' })).toBeDisabled()
    expect(screen.queryByRole('button', { name: '审核通过' })).not.toBeInTheDocument()
  })

  it('fails closed before transmitting when local recovery storage cannot be written', async () => {
    const f = fixture()
    render(<CurriculumSupplyReviewPanel child={child} request={f.adapter} />)
    const button = await screen.findByRole('button', { name: '独立校验' })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota') })
    fireEvent.click(button)
    expect(await screen.findByText(/尚未发送新操作/)).toBeInTheDocument()
    expect(f.adapter.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(0)
  })

  it('clears mounted in-memory notes and ignores a late write after host privacy deletion', async () => {
    const f = fixture(); let finish!: (value: unknown) => void
    const original = f.adapter.getMockImplementation()!
    f.adapter.mockImplementation((path, init) => init?.method === 'POST' ? new Promise(resolve => { finish = resolve }) : original(path, init))
    render(<CurriculumSupplyReviewPanel child={child} request={f.adapter} />)
    await screen.findByRole('button', { name: '独立校验' })
    fireEvent.change(screen.getByLabelText('审核备注'), { target: { value: '删除前私人备注' } })
    fireEvent.click(screen.getByRole('button', { name: '独立校验' }))
    act(() => clearCurriculumSupplyLocalState(child.child_id))
    await screen.findByRole('button', { name: '独立校验' })
    await act(async () => finish({ ...f.item, state: 'validated', version: 2 }))
    expect(screen.getByLabelText('审核备注')).toHaveValue('')
    expect(localStorage.getItem(curriculumLocalKey(child.child_id))).not.toContain('删除前私人备注')
    expect(screen.queryByRole('button', { name: '重试原请求' })).not.toBeInTheDocument()
  })
})


describe('paired-paper embedded review transport only',()=>{
  it('survives the actual renderer StrictMode layout and passive effect replay',async()=>{
    const f=fixture()
    render(<StrictMode><ParentPaperFormalQueue child={child} request={f.adapter}/></StrictMode>)
    expect(await screen.findByRole('button',{name:'独立校验'})).not.toBeDisabled()
    expect(screen.queryByText(/家长权限已变化。本机待核对审核操作保留/)).toBeNull()
  })
  afterEach(()=>{cleanup();localStorage.clear();vi.useRealTimers();vi.restoreAllMocks()})
  it('keeps a retired effect generation private when old publish resolves after reactivation',async()=>{
    const f=fixture();f.setItem({...f.item,state:'reviewed',version:3})
    const posts:{resolve:(v:unknown)=>void;signal:AbortSignal}[]=[]
    const request:CurriculumParentRequest=async(path,init)=>{
      if(init?.method==='POST')return new Promise(resolve=>posts.push({resolve,signal:init.signal as AbortSignal}))
      return f.adapter(path,init)
    }
    const element=(mode:'visible'|'hidden')=><StrictMode><Activity mode={mode}><ParentPaperFormalQueue child={child} request={request}/></Activity></StrictMode>
    const view=render(element('visible'))
    fireEvent.click(await screen.findByRole('button',{name:'发布到题库'}))
    await waitFor(()=>expect(posts).toHaveLength(1))
    const original=JSON.parse(localStorage.getItem(curriculumLocalKey(child.child_id))!).pending
    view.rerender(element('hidden'))
    expect(posts[0].signal.aborted).toBe(true)
    view.rerender(element('visible'))
    fireEvent.click(await screen.findByRole('button',{name:'重试原请求'}))
    await waitFor(()=>expect(posts).toHaveLength(2))
    await act(async()=>{posts[0].resolve({...f.item,state:'published',version:4,eligible:true})})
    expect(JSON.parse(localStorage.getItem(curriculumLocalKey(child.child_id))!).pending).toEqual(original)
    expect(posts[1].signal.aborted).toBe(false)
    expect(screen.getByRole('button',{name:'重试原请求'})).toBeDisabled()
    await act(async()=>{posts[1].resolve({...f.item,state:'published',version:4,eligible:true})})
    await waitFor(()=>expect(JSON.parse(localStorage.getItem(curriculumLocalKey(child.child_id))!).pending).toBeNull())
  })
  it.each([0,1,2,3,4])('keeps a completed old request fenced across effect reactivation at microtask depth %s',async depth=>{
    const f=fixture();f.setItem({...f.item,state:'reviewed',version:3})
    let atRetirement:unknown='not-yet',retired=false
    let view:ReturnType<typeof render>
    const element=(mode:'visible'|'hidden')=><StrictMode><Activity mode={mode}><ParentPaperFormalQueue child={child} request={request}/></Activity></StrictMode>
    const request:CurriculumParentRequest=async(path,init)=>{
      const value=await f.adapter(path,init)
      if(init?.method==='POST'&&path.endsWith('/publish')){
        const schedule=(left:number):void=>{queueMicrotask(()=>{if(left)schedule(left-1);else{
          atRetirement=JSON.parse(localStorage.getItem(curriculumLocalKey(child.child_id))!).pending
          flushSync(()=>view.rerender(element('hidden')))
          flushSync(()=>view.rerender(element('visible')))
          retired=true
        }})}
        schedule(depth)
      }
      return value
    }
    view=render(element('visible'))
    fireEvent.click(await screen.findByRole('button',{name:'发布到题库'}))
    await waitFor(()=>expect(retired).toBe(true))
    await waitFor(()=>expect(screen.getByRole('region',{name:'孩子甲的课程供给审核'})).toHaveAttribute('aria-busy','false'))
    const pending=JSON.parse(localStorage.getItem(curriculumLocalKey(child.child_id))!).pending
    expect(pending).toEqual(atRetirement)
    console.info('PUBLIC_SYNTHETIC_EFFECT_GENERATION_ADJACENCY',JSON.stringify({depth,pending_at_retirement:atRetirement!==null,pending_after_reactivation:pending!==null}))
  })
  it('keeps simultaneous reads independent while the original panel rejects an older focus',async()=>{
    const f=fixture(),pending:{resolve:(v:unknown)=>void;signal:AbortSignal}[]=[]
    const request:CurriculumParentRequest=async(path,init)=>{
      if(path.endsWith('/options')&&!path.includes('/sources/'))return new Promise(resolve=>pending.push({resolve,signal:init?.signal as AbortSignal}))
      return f.adapter(path,init)
    }
    const view=render(<ParentPaperFormalQueue child={child} request={request} focusRequest={{objectId:'candidate-a',nonce:'first'}}/>)
    await waitFor(()=>expect(pending).toHaveLength(1))
    view.rerender(<ParentPaperFormalQueue child={child} request={request} focusRequest={{objectId:'candidate-a',nonce:'second'}}/>)
    await waitFor(()=>expect(pending).toHaveLength(2))
    expect(pending.map(p=>p.signal.aborted)).toEqual([false,false])
    await act(async()=>{pending[1].resolve(f.options)})
    expect(await screen.findByRole('button',{name:'独立校验'})).not.toBeDisabled()
    expect(screen.getByRole('option',{name:/^已审核四年级教材/})).toBeInTheDocument()
    expect(pending[0].signal.aborted).toBe(false)
    await act(async()=>{pending[0].resolve({...f.options,sources:f.options.sources.map(source=>({...source,title:'陈旧读取不应覆盖当前教材'}))})})
    expect(screen.getByRole('button',{name:'独立校验'})).not.toBeDisabled()
    expect(screen.getByRole('option',{name:/^已审核四年级教材/})).toBeInTheDocument()
    expect(screen.queryByRole('option',{name:/^陈旧读取不应覆盖当前教材/})).toBeNull()
  })
  it('explicit stop retains the original persisted publish, and child switch isolates its late response',async()=>{
    const f=fixture();f.setItem({...f.item,state:'reviewed',version:3})
    let late!:(v:unknown)=>void,signal:AbortSignal|undefined
    const request:CurriculumParentRequest=async(path,init)=>{
      if(init?.method==='POST'){signal=init.signal??undefined;return new Promise(resolve=>{late=resolve})}
      return f.adapter(path,init)
    }
    const view=render(<ParentPaperFormalQueue child={child} request={request}/>)
    fireEvent.click(await screen.findByRole('button',{name:'发布到题库'}))
    await waitFor(()=>expect(late).toBeTypeOf('function'))
    const original=JSON.parse(localStorage.getItem(curriculumLocalKey(child.child_id))!).pending
    fireEvent.click(screen.getByRole('button',{name:'停止等待审核，保留原操作'}))
    await waitFor(()=>expect(screen.getByRole('button',{name:'重试原请求'})).not.toBeDisabled())
    expect(signal?.aborted).toBe(true)
    expect(JSON.parse(localStorage.getItem(curriculumLocalKey(child.child_id))!).pending).toEqual(original)
    view.rerender(<ParentPaperFormalQueue child={{...child,child_id:'child-b',nickname:'孩子乙'}} request={request}/>)
    await act(async()=>{late({...f.item,state:'published',version:4,eligible:true})})
    expect(JSON.parse(localStorage.getItem(curriculumLocalKey(child.child_id))!).pending).toEqual(original)
    expect(screen.queryByText(f.item.payload.prompt)).toBeNull()
    expect(localStorage.getItem(curriculumLocalKey('child-b'))).toBeNull()
  })
  it.each([1,2,3,4,5,6])('fences original panel continuations when authority changes at microtask depth %s',async depth=>{
    const f=fixture();f.setItem({...f.item,state:'reviewed',version:3})
    let atRevocation:unknown='not-yet',afterResult=false
    const request:CurriculumParentRequest=async(path,init)=>{
      const value=await f.adapter(path,init)
      if(init?.method==='POST'&&path.endsWith('/publish')){
        const schedule=(left:number):void=>{queueMicrotask(()=>{if(left)schedule(left-1);else{
          atRevocation=JSON.parse(localStorage.getItem(curriculumLocalKey(child.child_id))!).pending
          changeParentDraftAuthority('locked')
        }})}
        schedule(depth);afterResult=true
      }
      return value
    }
    render(<ParentPaperFormalQueue child={child} request={request}/>)
    fireEvent.click(await screen.findByRole('button',{name:'发布到题库'}))
    await waitFor(()=>expect(afterResult).toBe(true))
    await screen.findByText(/家长权限已变化。本机待核对审核操作保留/)
    const state=JSON.parse(localStorage.getItem(curriculumLocalKey(child.child_id))!)
    if(atRevocation!==null)expect(state.pending).toEqual(atRevocation)
    console.info('PUBLIC_SYNTHETIC_AUTHORITY_ADJACENCY',JSON.stringify({depth,pending_at_revocation:atRevocation!==null,pending_after_revocation:state.pending!==null}))
    expect(screen.queryByText(f.item.payload.prompt)).toBeNull()
  })
})
