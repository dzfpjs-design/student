import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { OralSourceAuthorPanel } from './OralSourceAuthorPanel'
import { ApiError } from './api'
import { CurriculumSupplyReviewPanel } from './CurriculumSupplyReviewPanel'
import { CurriculumOriginal, oralFamilyNames, type OralFamily, type OralSource } from './OralTaskPreview'
import { clearCurriculumSupplyLocalState, validateOralPackage } from './curriculumSupplyApi'
import { oralAuthorKey } from './oralSourceApi'
import type { CurriculumPackage, CurriculumParentRequest } from './curriculumSupplyTypes'

const child = { child_id: 'oral-child', nickname: '孩子甲', grade: '四年级' }
const local = { child_id: child.child_id, local_only: true, cloud_transmission_allowed: false }
const picture = { diagnostic_item_id: 'exact-picture', source_version: 'a'.repeat(64), stimulus_asset: 'pet-study-room' }
function original(family: OralFamily): OralSource {
  return { schema: 'pet_speaking_source_v1', subject: '英语', grade: child.grade, knowledge_node_id: 'exact-node',
    assessment_mode: 'pet_reference_rubric', answer_type: family === 'read_aloud' ? 'read_aloud' : 'speaking',
    task_family: family, prompt: 'Describe your class visit and explain why it matters.', task_points: ['Describe the visit', 'Explain one reason'],
    stimulus_text: 'First Ada visits the garden. Then she waters a plant. Finally she thanks her teacher.',
    stimulus_binding: family === 'picture_description' ? picture : null }
}
function fixture() {
  let content = ''
  const request = vi.fn<CurriculumParentRequest>(async (path, init) => {
    if (path.endsWith('/oral-authoring')) return { ...local, grade: child.grade, options: Object.keys(oralFamilyNames).map(k => {
      const o = original(k as OralFamily)
      return { task_family: o.task_family, title: oralFamilyNames[o.task_family], knowledge_node_id: o.knowledge_node_id,
        seed_item_id: 'seed-' + k, answer_type: o.answer_type, stimulus_binding: o.stimulus_binding }
    }) }
    if (init?.method === 'POST') { content = JSON.parse(String(init.body)).content_text; return { child_id: child.child_id, source_id: 'saved-source', review_status: 'approved' } }
    return { ...local, source_id: 'saved-source', excerpts: [{ text: content }] }
  })
  return { request, onSaved: vi.fn() }
}
async function fill(f: ReturnType<typeof fixture>) {
  render(<OralSourceAuthorPanel child={child} {...f} />)
  fireEvent.click(screen.getByRole('button', { name: '编写口语原题' }))
  await waitFor(() => expect(screen.getByLabelText('口语任务')).not.toBeDisabled())
  fireEvent.change(screen.getByLabelText('口语任务'), { target: { value: 'read_aloud' } })
  for (const [label, value] of [['资料名称', 'Garden passage'], ['资料版本', 'Original v1'], ['完整任务要求', 'Read this new garden passage aloud.'],
    ['完整阅读材料（必填）', original('read_aloud').stimulus_text], ['任务点 1', 'Read the entire passage']]) {
    fireEvent.change(screen.getByLabelText(label!), { target: { value } })
  }
  fireEvent.click(screen.getByRole('checkbox'))
}
afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks() })

it.each(Object.keys(oralFamilyNames) as OralFamily[])('shows complete %s with materials and no JSON', family => {
  const o = original(family)
  render(<CurriculumOriginal text={JSON.stringify(o)} />)
  expect(screen.getByText(o.prompt)).toBeVisible()
  expect(screen.getByText(o.stimulus_text!)).toBeVisible()
  o.task_points.forEach(p => expect(screen.getByText(p)).toBeVisible())
  expect(document.body.textContent).not.toContain('knowledge_node_id')
  expect(document.body.textContent).not.toContain('pet_speaking_source_v1')
  expect(screen.queryAllByRole('img')).toHaveLength(family === 'picture_description' ? 1 : 0)
})
it('authors legal exact typed text, reads back before success, and restores an unsent form', async () => {
  const f = fixture(); await fill(f)
  cleanup()
  render(<OralSourceAuthorPanel child={child} {...f} />)
  fireEvent.click(screen.getByRole('button', { name: '编写口语原题' }))
  await waitFor(() => expect(screen.getByRole('button', { name: '保存口语原题' })).not.toBeDisabled())
  expect(screen.getByLabelText('资料名称')).toHaveValue('Garden passage')
  expect(f.request.mock.calls.filter(([, i]) => i?.method)).toHaveLength(0)
  fireEvent.click(screen.getByRole('button', { name: '保存口语原题' }))
  await waitFor(() => expect(f.onSaved).toHaveBeenCalledWith('saved-source'))
  const body = JSON.parse(String(f.request.mock.calls.find(([, i]) => i?.method)?.[1]?.body))
  expect(JSON.parse(body.content_text)).toMatchObject({ answer_type: 'read_aloud', assessment_mode: 'pet_reference_rubric',
    stimulus_text: original('read_aloud').stimulus_text, task_points: ['Read the entire passage'] })
  expect(body).not.toHaveProperty('cloud_transmission_allowed')
})
it('preserves the actual pending payload across loss of response and remount', async () => {
  const f = fixture(), base = f.request.getMockImplementation()!
  let lost = true
  f.request.mockImplementation(async (p, i) => { const r = await base(p, i); if (i?.method && lost) { lost = false; throw Error('lost response') }; return r })
  await fill(f); fireEvent.click(screen.getByRole('button', { name: '保存口语原题' }))
  await screen.findByRole('alert'); cleanup()
  render(<OralSourceAuthorPanel child={child} {...f} />)
  fireEvent.click(screen.getByRole('button', { name: '编写口语原题' }))
  fireEvent.click(screen.getByRole('button', { name: '重试原保存请求' }))
  await waitFor(() => expect(f.onSaved).toHaveBeenCalledTimes(1))
  const posts = f.request.mock.calls.filter(([, i]) => i?.method)
  expect(posts).toHaveLength(2); expect(posts[0][1]!.body).toBe(posts[1][1]!.body)
})
it('privacy clear during import cannot recreate the private journal or focus', async () => {
  const f = fixture(), base = f.request.getMockImplementation()!
  let finish!: (v: unknown) => void
  f.request.mockImplementation((p, i) => i?.method ? new Promise(r => { finish = r }) : base(p, i))
  await fill(f); fireEvent.click(screen.getByRole('button', { name: '保存口语原题' }))
  act(() => clearCurriculumSupplyLocalState(child.child_id))
  await act(async () => { finish({ child_id: child.child_id, source_id: 'saved-source', review_status: 'approved' }) })
  expect(localStorage.getItem(oralAuthorKey(child.child_id))).toBeNull()
  expect(f.onSaved).not.toHaveBeenCalled()
})
it('storage failure prevents saving instead of losing the parent work silently', async () => {
  const f = fixture(); await fill(f)
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw Error('disk full') })
  fireEvent.click(screen.getByRole('button', { name: '保存口语原题' }))
  expect(screen.getByText('本机草稿无法保存，未发送新请求。')).toBeVisible()
  expect(f.request.mock.calls.filter(([, i]) => i?.method)).toHaveLength(0)
})
it('unknown image and altered full source block the publish adapter, not just a warning', () => {
  const s = original('picture_description')
  const pack = { payload: { validator: 'pet_speaking_rubric_source_v1', prompt: s.prompt, expected_answer: null, pet_oral: s },
    review_packet: { pet_oral: s, textbook: { content_text: JSON.stringify(s) } } } as CurriculumPackage
  expect(() => validateOralPackage(pack)).not.toThrow()
  pack.review_packet!.textbook.content_text = JSON.stringify({ ...s, stimulus_text: 'replaced' })
  expect(() => validateOralPackage(pack)).toThrow()
  s.stimulus_binding!.stimulus_asset = 'arbitrary-file'
  expect(() => validateOralPackage({ ...pack, review_packet: null })).toThrow()
})
it('imports a complete text file into material without treating it as the task or granting permission', async () => {
  const f = fixture(); await fill(f)
  const file = new File(['text'], 'garden.txt', { type: 'text/plain' })
  Object.defineProperty(file, 'text', { value: async () => original('read_aloud').stimulus_text + '\nComplete final line.' })
  fireEvent.change(screen.getByLabelText('导入纯文本材料'), { target: { files: [file] } })
  await waitFor(() => expect(screen.getByLabelText('完整阅读材料（必填）')).toHaveValue(original('read_aloud').stimulus_text + '\nComplete final line.'))
  expect(screen.getByRole('checkbox')).not.toBeChecked()
  expect(screen.getByLabelText('完整任务要求')).toHaveValue('Read this new garden passage aloud.')
  expect(f.request.mock.calls.filter(([, i]) => i?.method)).toHaveLength(0)
})
it('a late import reply after switching children cannot select a source or change the other journal', async () => {
  const f = fixture(), base = f.request.getMockImplementation()!
  let finish!: (v: unknown) => void
  f.request.mockImplementation((p, i) => i?.method ? new Promise(r => { finish = r }) : base(p, i))
  await fill(f); fireEvent.click(screen.getByRole('button', { name: '保存口语原题' }))
  cleanup()
  render(<OralSourceAuthorPanel child={{ ...child, child_id: 'other-child' }} {...f} />)
  await act(async () => finish({ child_id: child.child_id, source_id: 'saved-source', review_status: 'approved' }))
  expect(f.onSaved).not.toHaveBeenCalled()
  expect(localStorage.getItem(oralAuthorKey('other-child'))).toBeNull()
  expect(JSON.parse(localStorage.getItem(oralAuthorKey(child.child_id))!).pending).not.toBeNull()
})
it('an oral excerpt cannot offer unrelated listening validators in the real review panel', async () => {
  const o = original('read_aloud')
  const request: CurriculumParentRequest = async path => {
    if (path.endsWith('/sources/book/options')) return { ...local, source_id: 'book', standards: [],
      excerpts: [{ excerpt_id: 'e', sequence: 1, text: JSON.stringify(o) }], seeds: [
        { seed_item_id: 'unrelated', title: '不相关听力', prompt: 'Listen to this', validator: 'pet_reviewed_source_v1', stages: ['parallel'] },
        { seed_item_id: 'oral', title: '朗读原型', prompt: 'Read this', validator: 'pet_speaking_rubric_source_v1', stages: ['parallel'] }] }
    if (path.endsWith('/options')) return { ...local, grade: child.grade, semesters: ['上学期'], sources: [
      { source_id: 'book', title: 'Original oral', subject: '英语', edition: 'v1', available: true, blockers: [], excerpt_count: 1 }] }
    return { ...local, current_curriculum: [], blocked_job_counts: {}, eligible_curriculum_items: 0, qualified_stages: [], packages: [] }
  }
  render(<CurriculumSupplyReviewPanel child={child} request={request} />)
  await waitFor(() => expect(screen.getByLabelText('当前教材')).not.toBeDisabled())
  fireEvent.change(screen.getByLabelText('当前教材'), { target: { value: 'book' } })
  await waitFor(() => expect(screen.getByLabelText('教材题目')).not.toBeDisabled())
  fireEvent.change(screen.getByLabelText('教材题目'), { target: { value: 'e' } })
  expect(screen.queryByRole('option', { name: /不相关听力/ })).not.toBeInTheDocument()
  expect(screen.getByRole('option', { name: /朗读原型/ })).toBeInTheDocument()
})
it('allows an explicit return from a stale pending request without silently reauthorizing it', async () => {
  const f = fixture(), base = f.request.getMockImplementation()!
  f.request.mockImplementation((p, i) => i?.method ? Promise.reject(new ApiError('来源版本已变更', 409)) : base(p, i))
  await fill(f); fireEvent.click(screen.getByRole('button', { name: '保存口语原题' }))
  await screen.findByText('来源版本已变更')
  fireEvent.click(screen.getByRole('button', { name: '放弃重试，返回草稿' }))
  await waitFor(() => expect(screen.getByLabelText('口语任务')).not.toBeDisabled())
  expect(screen.getByRole('checkbox')).not.toBeChecked()
  expect(screen.getByLabelText('完整阅读材料（必填）')).toHaveValue(original('read_aloud').stimulus_text)
  expect(f.request.mock.calls.filter(([, i]) => i?.method)).toHaveLength(1)
  expect(f.onSaved).not.toHaveBeenCalled()
})
