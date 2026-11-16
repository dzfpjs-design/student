import { cleanup, fireEvent, render, screen, waitFor, act } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { CurriculumSupplyReviewPanel } from './CurriculumSupplyReviewPanel'
import { clearCurriculumSupplyLocalState, curriculumLocalKey, curriculumReason } from './curriculumSupplyApi'
import type { CurriculumAudit, CurriculumPackage, CurriculumParentRequest, CurriculumSourceOptions } from './curriculumSupplyTypes'

const child = { child_id: 'junior', nickname: '孩子乙', grade: '初三' }
const local = { local_only: true as const, cloud_transmission_allowed: false as const }
const prompt = '用求根公式解 x²-5x+6=0，写出两个根。'
const quote = '理解一元二次方程求根公式并求出方程的根。'
function fixture(tuple = false) {
  const scope = { grade: '初三', subject: '数学', edition: '合成教材', semester: '上学期', standard_scope: '第四学段', textbook_source_id: 'book' }
  const source: CurriculumSourceOptions = { ...local, child_id: child.child_id, source_id: 'book',
    seeds: [], first_sources: [{ knowledge_node_id: 'formula', title: '一元二次方程求根公式', prompt, excerpt_id: 'excerpt' }],
    excerpts: [{ excerpt_id: 'excerpt', sequence: 1, text: JSON.stringify({ schema: 'curriculum_objective_source_v1', prompt,
      expected_answer: { exact_answers: ['2,3'], ...(tuple ? { answer_format: 'reviewed_spoken_numeric_tuple_v1', numeric_kind: 'integer', tuple_size: 2 } : {}) } }) }],
    standards: [{ requirement_id: 'req', title: '求根公式', stage_scope: '第四学段', requirement_text: quote,
      source_title: '已审核合成课标', page_number: 12, quote, available: true, blockers: [] }] }
  let item: CurriculumPackage | null = null
  let lose = false
  const commands: string[] = []
  const request = vi.fn<CurriculumParentRequest>(async (path, init) => {
    if (path.endsWith('/oral-authoring')) return { ...local, child_id: child.child_id, options: [] }
    if (!init?.method) {
      if (path.endsWith('/sources/book/options')) return source
      if (path.endsWith('/options')) return { ...local, child_id: child.child_id, grade: child.grade, semesters: ['上学期'],
        sources: [{ source_id: 'book', title: '合成完整原题', subject: '数学', edition: '合成教材', available: true, blockers: [], excerpt_count: 1 }] }
      if (path.endsWith('/audit')) return { ...local, child_id: child.child_id, current_curriculum: [{ version: 1, scope }],
        blocked_job_counts: {}, eligible_curriculum_items: Number(item?.eligible ?? false), qualified_stages: [],
        packages: item ? [{ ...item, prompt }] : [], thinking_or_transfer_coverage_claim_allowed: false } satisfies CurriculumAudit
      return item
    }
    const body = JSON.parse(String(init.body))
    commands.push(body.action_id)
    if (path.endsWith('/first-sources')) {
      expect(body.source).not.toHaveProperty('seed_item_id')
      item ??= { ...local, candidate_id: 'first-original', child_id: child.child_id, version: 1, state: 'draft', eligible: false,
        blockers: [], next_action: 'independent_validate', supersedes: null, diagnostic_item_id: null,
        payload: { ...body.source, seed_item_id: null, stage: 'parallel', validator: 'curriculum_objective_exact_source_v1', prompt, expected_answer: null },
        review_packet: { fingerprint: 'source1', scope, textbook: { source_id: 'book', title: '合成原題', version_label: '合成教材',
          source_ref: 'not-for-display', review_status: 'approved', excerpt_id: 'excerpt', content_text: source.excerpts[0].text },
          standard: { source_id: 'std', official_url: 'isolated:std', page_number: 12, requirement_text: quote, quote }, review_scope: 'baseline_only' },
        stage_review_context: null, events: [] }
    } else {
      expect(item).not.toBeNull()
      expect(body.expected_version).toBe(item!.version)
      const kind = path.split('/').at(-1)!
      const states = { validate: 'validated', review: 'reviewed', publish: 'published', withdraw: 'withdrawn' } as const
      const state = states[kind as keyof typeof states]
      item = { ...item!, state, version: item!.version+1, eligible: state === 'published', diagnostic_item_id: state === 'published' ? 'diag-first-original' : item!.diagnostic_item_id }
      if (tuple && kind === 'validate') item.events.push({ kind: 'validate', actor: 'isolated-reviewer', version: item.version, created_at: '2026-10-07T00:00:00Z',
        result: { state, blockers: [], check: { validator: 'curriculum_objective_exact_source_v1', computed_answer: '2,3', question_hash: 'source-question', payload_hash: 'reviewed-tuple',
          expected_answer: { answer_format: 'reviewed_spoken_numeric_tuple_v1', numeric_kind: 'integer', tuple_size: 2 } } } })
    }
    if (lose) { lose = false; throw new Error('isolated lost response') }
    return item
  })
  return { request, source, commands, loseNext: () => { lose = true }, current: () => item }
}
afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks() })
it('explains restored approval separately from a source failure without exposing an internal code', () => {
  expect(curriculumReason('CURRICULUM_FIRST_REVIEW_AFTER_RESTORE')).toContain('旧发布操作不能恢复资格')
  expect(curriculumReason('CURRICULUM_FIRST_BASELINE_ONLY')).toContain('不能直接认定为近迁移或远迁移')
})
async function fill() {
  await screen.findByRole('option', { name: /已审核合成课标/ })
  fireEvent.change(screen.getByLabelText('对照课标'), { target: { value: 'req:12' } })
  fireEvent.change(screen.getByLabelText('教材题目'), { target: { value: 'excerpt' } })
  fireEvent.change(screen.getByLabelText('题目难度（1至5）'), { target: { value: '1' } })
  fireEvent.change(screen.getByLabelText('解题说明'), { target: { value: '由原材料完整核对求根公式及参考答案。' } })
}
it('shows the reviewed ordered numeric rule next to the original answer before approval', async () => {
  const f = fixture(true)
  render(<CurriculumSupplyReviewPanel child={child} request={f.request} />)
  await fill()
  fireEvent.click(screen.getByRole('button', { name: '建立原来源首题草案' }))
  await screen.findByLabelText('审核备注')
  fireEvent.click(screen.getByRole('button', { name: /独立校验/ }))
  await screen.findByText('口述数值评分：')
  expect(screen.getByText(/按原题顺序核对2个整数/)).toBeInTheDocument()
  expect(screen.getByText(/不自动提取数字/)).toBeInTheDocument()
  expect(f.current()?.state).toBe('validated')
})
it('uses the mounted existing review path from seedless original to publish, reload and withdraw', async () => {
  const f = fixture()
  const view = render(<CurriculumSupplyReviewPanel child={child} request={f.request} />)
  await fill()
  expect(screen.getByRole('option', { name: prompt })).toBeInTheDocument()
  expect(screen.getByText('原题完整参考答案')).toBeInTheDocument()
  expect(screen.queryByText(/"schema".*curriculum_objective_source/)).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '建立原来源首题草案' }))
  await screen.findByLabelText('审核备注')
  fireEvent.click(screen.getByRole('button', { name: /独立校验/ }))
  await waitFor(() => expect(f.current()?.state).toBe('validated'))
  fireEvent.change(screen.getByLabelText('审核备注'), { target: { value: '核对原题、节点和完整答案，不认定迁移。' } })
  fireEvent.click(screen.getByRole('button', { name: '审核通过' }))
  await waitFor(() => expect(f.current()?.state).toBe('reviewed'))
  await waitFor(() => expect(screen.getByRole('button', { name: '发布到题库' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '发布到题库' }))
  await waitFor(() => expect(f.current()?.eligible).toBe(true))
  view.unmount()
  render(<CurriculumSupplyReviewPanel child={child} request={f.request} />)
  await screen.findByText('当前有效题目：1道')
  fireEvent.change(screen.getByLabelText('审核备注'), { target: { value: '原题撤回' } })
  fireEvent.click(screen.getByRole('button', { name: '撤回题目' }))
  await screen.findByText('当前有效题目：0道')
  expect(f.current()?.state).toBe('withdrawn')
})
it('keeps the exact seedless command across lost response and explicit restart retry', async () => {
  const f = fixture()
  const view = render(<CurriculumSupplyReviewPanel child={child} request={f.request} />)
  await fill()
  f.loseNext()
  fireEvent.click(screen.getByRole('button', { name: '建立原来源首题草案' }))
  await screen.findByRole('button', { name: '重试原请求' })
  await waitFor(() => expect(f.commands).toHaveLength(1))
  view.unmount()
  render(<CurriculumSupplyReviewPanel child={child} request={f.request} />)
  const retry = await screen.findByRole('button', { name: '重试原请求' })
  expect(f.commands).toHaveLength(1)
  fireEvent.click(retry)
  await waitFor(() => expect(f.commands).toHaveLength(2))
  expect(f.commands[1]).toBe(f.commands[0])
  await waitFor(() => expect(JSON.parse(localStorage.getItem(curriculumLocalKey(child.child_id))!).pending).toBeNull())
})
it('clears only the chosen child command and never transplants a pending first source', async () => {
  const f = fixture()
  const view = render(<CurriculumSupplyReviewPanel child={child} request={f.request} />)
  await fill()
  f.loseNext()
  fireEvent.click(screen.getByRole('button', { name: '建立原来源首题草案' }))
  await waitFor(() => expect(f.commands).toHaveLength(1))
  localStorage.setItem(curriculumLocalKey('other'), 'preserve other child')
  act(() => clearCurriculumSupplyLocalState(child.child_id))
  expect(localStorage.getItem(curriculumLocalKey('other'))).toBe('preserve other child')
  view.unmount()
  expect(f.commands).toHaveLength(1)
})
