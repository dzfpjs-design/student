import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './api'
import { WritingQuestionAuthorizationPanel } from './WritingQuestionAuthorizationPanel'
import { clearCurriculumSupplyLocalState, clearWritingAuthorizationLocalState, createWritingAuthorizationApi, writingAuthorizationLocalKey } from './curriculumSupplyApi'
import type { WritingAuthorization, WritingAuthorizationCommand } from './curriculumSupplyApi'
import type { CurriculumParentRequest } from './curriculumSupplyTypes'

const child = { child_id: 'child-a', nickname: '孩子甲', grade: '四年级' }
const other = { ...child, child_id: 'child-b', nickname: '孩子乙' }
const item = 'writing-a'
const key = writingAuthorizationLocalKey(child.child_id, item)
const evidence = '这道题由我独立创作，确认允许按所列题目回答评分标准的文字用途处理。'
function required(id = child.child_id): WritingAuthorization {
  return { child_id: id, diagnostic_item_id: item, revision: 0, status: 'authorization_required', reason: null,
    item_version: 'item-v1', source_version: 'source-v1', curriculum_version: 'curriculum-v1', purpose_version: 'writing_question_formative_text_v1',
    preview: { question: `${id === child.child_id ? '甲' : '乙'}的原题：Write an email to your friend.`, task_points: ['Describe your plan.'] },
    authorization_id: null, license_options: ['CC0-1.0', 'original_author', 'explicit_cloud_processing_permission'],
    rights_review_authority: 'parent_declaration_not_legal_or_network_verification', local_only: true, cloud_transmission_allowed: false }
}
function fixture() {
  let current = required()
  let lose = false
  const receipts = new Map<string, { body: string; receipt: { event_id: string; revision: number; command: string } }>()
  const request = vi.fn<CurriculumParentRequest>(async (path, init) => {
    const id = decodeURIComponent(path.split('/')[4])
    if (!init?.method) return structuredClone(id === child.child_id ? current : required(id))
    expect(id).toBe(child.child_id)
    const body = JSON.parse(String(init.body))
    expect(body).not.toHaveProperty('actor')
    expect(body).not.toHaveProperty('actor_id')
    const kind = path.split('/').at(-1)!
    const previous = receipts.get(body.client_action_id)
    if (previous && previous.body !== init.body) throw new ApiError('conflict', 409)
    if (!previous) {
      if (body.expected_revision !== current.revision) throw new ApiError('conflict', 409)
      current = { ...current, revision: current.revision + 1, status: kind === 'grant' ? 'authorized' : 'revoked',
        authorization_id: kind === 'grant' ? `event-${current.revision + 1}` : null }
      receipts.set(body.client_action_id, { body: String(init.body), receipt: { event_id: `event-${current.revision}`, revision: current.revision, command: kind } })
    }
    if (lose) { lose = false; throw new TypeError('lost response') }
    return { receipt: receipts.get(body.client_action_id)!.receipt, current: structuredClone(current) }
  })
  return { request, set: (v: WritingAuthorization) => { current = v }, read: () => current, lose: () => { lose = true } }
}
async function fill() {
  await screen.findByLabelText('权利依据')
  fireEvent.change(screen.getByLabelText('权利依据'), { target: { value: 'original_author' } })
  fireEvent.change(screen.getByLabelText('许可链接或声明名称'), { target: { value: '我的独立原题创作声明' } })
  fireEvent.change(screen.getByLabelText('权利依据说明（至少20字）'), { target: { value: evidence } })
  fireEvent.click(screen.getByRole('checkbox'))
}
function postCalls(f: ReturnType<typeof fixture>) { return f.request.mock.calls.filter(([, init]) => init?.method === 'POST') }

function materialQuestion(): WritingAuthorization {
  return { ...required(), purpose_version: 'writing_question_material_formative_text_v2',
    preview: { question: '比较两份材料，解释用水量变化。\n\n甲地节水记录。\n乙地用水记录。', task_points: ['引用两份材料解释差异。'],
      materials: ['甲地节水记录', '乙地用水记录'].map((title, index) => ({ material_key: `material-${index}`,
        text: `${title}：三个月的家庭用水量分别为10、9、8吨。`, title, license_status: 'public',
        source_version: `material-version-${index}`, text_hash: `hash-${index}` })) } }
}
function fillGroup(group: HTMLElement, name: string) {
  const fields = within(group)
  fireEvent.change(fields.getByLabelText('权利依据'), { target: { value: 'original_author' } })
  fireEvent.change(fields.getByLabelText('许可链接或声明名称'), { target: { value: name } })
  fireEvent.change(fields.getByLabelText('权利依据说明（至少20字）'), { target: { value: `${name}由我独立创作，确认仅允许本次列明的文字用途处理，不包括其他资料。` } })
  fireEvent.click(fields.getByRole('checkbox'))
}

describe('WritingQuestionAuthorizationPanel persisted parent decisions', () => {
  afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks() })

  it('requires every material declaration independently and freezes ordered bindings in a schema 2 pending request', async () => {
    const f = fixture(); f.set(materialQuestion()); f.lose()
    render(<WritingQuestionAuthorizationPanel child={child} diagnosticItemId={item} request={f.request} />)
    const question = await screen.findByRole('group', { name: '题干权利声明' })
    expect(screen.getByText(/比较两份材料/)).toBeInTheDocument()
    fillGroup(question, '题干声明')
    expect(screen.getByRole('button', { name: '确认用途授权' })).toBeDisabled()
    fillGroup(screen.getByRole('group', { name: '材料1：甲地节水记录' }), '甲材料声明')
    expect(screen.getByRole('button', { name: '确认用途授权' })).toBeDisabled()
    fillGroup(screen.getByRole('group', { name: '材料2：乙地用水记录' }), '乙材料声明')
    fireEvent.click(screen.getByRole('button', { name: '确认用途授权' }))
    await screen.findByText(/未能核对结果/)
    const saved = JSON.parse(localStorage.getItem(key)!)
    expect(saved.schema).toBe(2)
    expect(saved.pending.body.material_license_bases).toEqual(materialQuestion().preview!.materials!.map((m, index) => ({
      material_key: m.material_key, source_version: m.source_version, text_hash: m.text_hash,
      license_basis: { license_id: 'original_author', reference: `${index ? '乙' : '甲'}材料声明`, evidence: expect.any(String) }
    })))
    expect(saved.pending.body).not.toHaveProperty('purpose_version')
    expect(postCalls(f)).toHaveLength(1)
  })

  it('clears all material declarations when one material changes even if the question versions do not', async () => {
    const f = fixture(); f.set(materialQuestion())
    render(<WritingQuestionAuthorizationPanel child={child} diagnosticItemId={item} request={f.request} />)
    fillGroup(await screen.findByRole('group', { name: '题干权利声明' }), '题干声明')
    fillGroup(screen.getByRole('group', { name: '材料1：甲地节水记录' }), '甲材料声明')
    const changed = materialQuestion(); changed.preview!.materials![0].text_hash = 'changed-hash'
    f.set(changed)
    fireEvent.click(screen.getByRole('button', { name: '刷新用途授权' }))
    await waitFor(() => expect(localStorage.getItem(key)).toBeNull())
    expect(screen.getAllByRole('checkbox').every(input => !(input as HTMLInputElement).checked)).toBe(true)
    expect(screen.getAllByRole('combobox').every(input => !(input as HTMLSelectElement).value)).toBe(true)
    expect(postCalls(f)).toHaveLength(0)
  })

  it('recovers the material request byte-for-byte after lost response and remount without reviving a revoked grant', async () => {
    const f = fixture(); f.set(materialQuestion()); f.lose()
    const mounted = render(<WritingQuestionAuthorizationPanel child={child} diagnosticItemId={item} request={f.request} />)
    fillGroup(await screen.findByRole('group', { name: '题干权利声明' }), '题干声明')
    fillGroup(screen.getByRole('group', { name: '材料1：甲地节水记录' }), '甲材料声明')
    fillGroup(screen.getByRole('group', { name: '材料2：乙地用水记录' }), '乙材料声明')
    fireEvent.click(screen.getByRole('button', { name: '确认用途授权' }))
    await screen.findByText(/未能核对结果/)
    const original = postCalls(f)[0]; mounted.unmount()
    f.set({ ...f.read(), status: 'revoked', authorization_id: null, revision: 2 })
    render(<WritingQuestionAuthorizationPanel child={child} diagnosticItemId={item} request={f.request} />)
    await screen.findByText('当前授权已撤销')
    expect(postCalls(f)).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: '重试原授权请求' }))
    await waitFor(() => expect(localStorage.getItem(key)).toBeNull())
    expect(postCalls(f)[1]).toEqual(original)
    expect(screen.queryByText('当前已授权指定用途')).not.toBeInTheDocument()
  })

  it.each(['source_changed', 'restore', 'tampered_pending'])('does not replay a material declaration after %s', async fault => {
    const f = fixture(); f.set(materialQuestion()); f.lose()
    const mounted = render(<WritingQuestionAuthorizationPanel child={child} diagnosticItemId={item} request={f.request} />)
    fillGroup(await screen.findByRole('group', { name: '题干权利声明' }), '题干声明')
    fillGroup(screen.getByRole('group', { name: '材料1：甲地节水记录' }), '甲材料声明')
    fillGroup(screen.getByRole('group', { name: '材料2：乙地用水记录' }), '乙材料声明')
    fireEvent.click(screen.getByRole('button', { name: '确认用途授权' }))
    await screen.findByText(/未能核对结果/); mounted.unmount()
    if (fault === 'tampered_pending') {
      const stored = JSON.parse(localStorage.getItem(key)!)
      delete stored.pending.body.material_license_bases
      localStorage.setItem(key, JSON.stringify(stored))
    } else {
      const updated = { ...f.read(), status: 'invalidated' as const, authorization_id: null }
      if (fault === 'restore') updated.reason = 'WRITING_QUESTION_RESTORE_REAUTHORIZATION_REQUIRED'
      else updated.preview!.materials![1].source_version = 'replacement-material'
      f.set(updated)
    }
    render(<WritingQuestionAuthorizationPanel child={child} diagnosticItemId={item} request={f.request} />)
    if (fault === 'tampered_pending') await screen.findByText(/本机恢复记录损坏/)
    else await waitFor(() => expect(localStorage.getItem(key)).toBeNull())
    expect(screen.queryByRole('button', { name: '重试原授权请求' })).not.toBeInTheDocument()
    expect(postCalls(f)).toHaveLength(1)
  })

  it.each(['v1', 'v2'])('does not upgrade a legacy schema 1 pending request when the current purpose is %s', async purpose => {
    const f = fixture(); f.lose()
    const mounted = render(<WritingQuestionAuthorizationPanel child={child} diagnosticItemId={item} request={f.request} />)
    await fill(); fireEvent.click(screen.getByRole('button', { name: '确认用途授权' }))
    await screen.findByText(/未能核对结果/); mounted.unmount()
    const saved = JSON.parse(localStorage.getItem(key)!)
    saved.schema = 1; delete saved.draft.materials
    saved.draft.binding = JSON.stringify(['item-v1', 'source-v1', 'curriculum-v1'])
    localStorage.setItem(key, JSON.stringify(saved))
    if (purpose === 'v2') f.set(materialQuestion())
    render(<WritingQuestionAuthorizationPanel child={child} diagnosticItemId={item} request={f.request} />)
    await screen.findByText(purpose === 'v1' ? '当前已授权指定用途' : '尚未授权此题用途')
    expect(postCalls(f)).toHaveLength(1)
    if (purpose === 'v1') {
      expect(JSON.parse(localStorage.getItem(key)!)).toEqual(saved)
      fireEvent.click(screen.getByRole('button', { name: '重试原授权请求' }))
      await waitFor(() => expect(localStorage.getItem(key)).toBeNull())
      expect(postCalls(f)[1]).toEqual(postCalls(f)[0])
      expect(JSON.parse(String(postCalls(f)[1][1]?.body))).not.toHaveProperty('material_license_bases')
    } else {
      expect(localStorage.getItem(key)).toBeNull()
      expect(screen.queryByRole('button', { name: '重试原授权请求' })).not.toBeInTheDocument()
      expect(screen.getAllByRole('checkbox').every(input => !(input as HTMLInputElement).checked)).toBe(true)
    }
  })

  it.each(['duplicate', 'missing', 'unknown_purpose', 'private', 'nested_actor'])('rejects malformed material read: %s', async mode => {
    const data = materialQuestion()
    if (mode === 'duplicate') data.preview!.materials![1] = data.preview!.materials![0]
    if (mode === 'missing') delete data.preview!.materials
    if (mode === 'unknown_purpose') Object.assign(data, { purpose_version: 'unknown' })
    if (mode === 'private') Object.assign(data.preview!.materials![0], { license_status: 'private' })
    if (mode === 'nested_actor') Object.assign(data.preview!.materials![0], { actor_id: 'forged' })
    const request = vi.fn<CurriculumParentRequest>().mockResolvedValue(data)
    await expect(createWritingAuthorizationApi(request).read(child.child_id, item)).rejects.toThrow('无法核对')
  })

  it.each(['duplicate', 'empty', 'nested_actor', 'short_evidence'])('refuses malformed material command before the trusted request: %s', async mode => {
    const materials = materialQuestion().preview!.materials!.map(m => ({ material_key: m.material_key,
      source_version: m.source_version, text_hash: m.text_hash,
      license_basis: { license_id: 'original_author' as const, reference: '独立声明', evidence } }))
    if (mode === 'duplicate') materials[1] = materials[0]
    if (mode === 'empty') materials.splice(0)
    if (mode === 'nested_actor') Object.assign(materials[0].license_basis, { actor_id: 'forged' })
    if (mode === 'short_evidence') materials[0].license_basis.evidence = 'short'
    const command: WritingAuthorizationCommand = { child_id: child.child_id, diagnostic_item_id: item, kind: 'grant', body: {
      expected_revision: 0, client_action_id: 'same-key', item_version: 'i1', source_version: 's1', curriculum_version: 'c1',
      license_basis: { license_id: 'original_author', reference: '题干声明', evidence }, material_license_bases: materials } }
    const request = vi.fn<CurriculumParentRequest>()
    await expect(createWritingAuthorizationApi(request).perform(child.child_id, item, command)).rejects.toThrow('无法核对')
    expect(request).not.toHaveBeenCalled()
  })

  it('clears schema 2 material drafts by child and all-child restore while preserving unrelated pending work', async () => {
    const f = fixture(); f.set(materialQuestion())
    render(<WritingQuestionAuthorizationPanel child={child} diagnosticItemId={item} request={f.request} />)
    fillGroup(await screen.findByRole('group', { name: '材料1：甲地节水记录' }), '甲材料声明')
    localStorage.setItem(writingAuthorizationLocalKey(other.child_id, item), 'other')
    localStorage.setItem('hsc-rubric-pending:unrelated', 'keep')
    act(() => clearWritingAuthorizationLocalState(child.child_id))
    await waitFor(() => expect(screen.getAllByRole('textbox').every(input => !(input as HTMLInputElement).value)).toBe(true))
    expect(localStorage.getItem(writingAuthorizationLocalKey(other.child_id, item))).toBe('other')
    act(() => clearWritingAuthorizationLocalState())
    await screen.findByRole('group', { name: '材料1：甲地节水记录' })
    expect(Object.keys(localStorage)).toEqual(['hsc-rubric-pending:unrelated'])
    expect(postCalls(f)).toHaveLength(0)
  })

  it('shows the original question and three rights, sends frozen versions without actor, and revokes using the current CAS', async () => {
    const f = fixture()
    render(<WritingQuestionAuthorizationPanel child={child} diagnosticItemId={item} request={f.request} />)
    await screen.findByText(required().preview!.question)
    expect(screen.getAllByRole('option')).toHaveLength(4)
    expect(screen.getByText(/系统不自动核验法律效力/)).toBeInTheDocument()
    expect(screen.getByText(/孩子本次回答与评分标准/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '确认用途授权' })).toBeDisabled()
    await fill()
    fireEvent.click(screen.getByRole('button', { name: '确认用途授权' }))
    await screen.findByText('当前已授权指定用途')
    const [path, init] = postCalls(f)[0]
    expect(path).toBe('/v1/parent/children/child-a/writing-question-authorizations/writing-a/grant')
    expect(init?.cache).toBe('no-store')
    expect(JSON.parse(String(init?.body))).toEqual({ expected_revision: 0, client_action_id: expect.any(String),
      item_version: 'item-v1', source_version: 'source-v1', curriculum_version: 'curriculum-v1',
      license_basis: { license_id: 'original_author', reference: '我的独立原题创作声明', evidence } })
    expect(localStorage.getItem(key)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '撤销此题用途授权' }))
    await screen.findByText('当前授权已撤销')
    expect(JSON.parse(String(postCalls(f)[1][1]?.body))).toEqual({ expected_revision: 1, client_action_id: expect.any(String) })
  })

  it('validates CC0 links and requires a fresh declaration after evidence changes', async () => {
    const f = fixture()
    render(<WritingQuestionAuthorizationPanel child={child} diagnosticItemId={item} request={f.request} />)
    await fill()
    fireEvent.change(screen.getByLabelText('权利依据'), { target: { value: 'CC0-1.0' } })
    fireEvent.click(screen.getByRole('checkbox'))
    expect(screen.getByRole('button', { name: '确认用途授权' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('CC0 声明的 HTTPS 链接'), { target: { value: 'https://example.invalid/license' } })
    expect(screen.getByRole('checkbox')).not.toBeChecked()
    fireEvent.click(screen.getByRole('checkbox'))
    expect(screen.getByRole('button', { name: '确认用途授权' })).toBeEnabled()
    fireEvent.change(screen.getByLabelText('CC0 声明的 HTTPS 链接'), { target: { value: 'https://secret@example.invalid/license' } })
    fireEvent.click(screen.getByRole('checkbox'))
    expect(screen.getByRole('button', { name: '确认用途授权' })).toBeDisabled()
    expect(postCalls(f)).toHaveLength(0)
  })

  it('persists a lost-response exact request across remount without autosend and uses current revoked over an old grant receipt', async () => {
    const f = fixture(); f.lose()
    const first = render(<WritingQuestionAuthorizationPanel child={child} diagnosticItemId={item} request={f.request} />)
    await fill(); fireEvent.click(screen.getByRole('button', { name: '确认用途授权' }))
    await screen.findByText(/未能核对结果/)
    const original = postCalls(f)[0]
    expect(JSON.parse(localStorage.getItem(key)!).pending.body).toEqual(JSON.parse(String(original[1]?.body)))
    first.unmount()
    // A separate authorized action has since revoked the grant; the old receipt is still durable.
    f.set({ ...f.read(), revision: 2, status: 'revoked', authorization_id: null })
    render(<WritingQuestionAuthorizationPanel child={child} diagnosticItemId={item} request={f.request} />)
    await screen.findByText('当前授权已撤销')
    expect(postCalls(f)).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: '重试原授权请求' }))
    await waitFor(() => expect(localStorage.getItem(key)).toBeNull())
    expect(postCalls(f)[1]).toEqual(original)
    expect(screen.getByText('当前授权已撤销')).toBeInTheDocument()
    expect(screen.queryByText('当前已授权指定用途')).not.toBeInTheDocument()
    expect(f.read().revision).toBe(2)
  })

  it.each(['invalidated', 'restore', 'version'])('discards obsolete permission drafts and pending on %s without replay', async mode => {
    const f = fixture(); f.lose()
    const first = render(<WritingQuestionAuthorizationPanel child={child} diagnosticItemId={item} request={f.request} />)
    await fill(); fireEvent.click(screen.getByRole('button', { name: '确认用途授权' }))
    await screen.findByText(/未能核对结果/); first.unmount()
    f.set({ ...f.read(), status: mode === 'invalidated' ? 'invalidated' : 'revoked', authorization_id: null,
      reason: mode === 'restore' ? 'WRITING_QUESTION_RESTORE_REAUTHORIZATION_REQUIRED' : null,
      ...(mode === 'version' ? { source_version: 'source-v2' } : {}) })
    render(<WritingQuestionAuthorizationPanel child={child} diagnosticItemId={item} request={f.request} />)
    await waitFor(() => expect(localStorage.getItem(key)).toBeNull())
    expect(screen.queryByRole('button', { name: '重试原授权请求' })).not.toBeInTheDocument()
    expect(screen.getByLabelText('权利依据')).toHaveValue('')
    expect(postCalls(f)).toHaveLength(1)
  })

  it('refreshes on CAS conflict but never rebases and resubmits an old decision', async () => {
    const f = fixture()
    render(<WritingQuestionAuthorizationPanel child={child} diagnosticItemId={item} request={f.request} />)
    await fill()
    f.set({ ...required(), revision: 2, status: 'revoked', source_version: 'source-v2' })
    fireEvent.click(screen.getByRole('button', { name: '确认用途授权' }))
    await screen.findByText(/版本或来源已变化/)
    await waitFor(() => expect(localStorage.getItem(key)).toBeNull())
    expect(postCalls(f)).toHaveLength(1)
    expect(screen.getByLabelText('权利依据')).toHaveValue('')
    expect(screen.getByRole('button', { name: '确认用途授权' })).toBeDisabled()
  })

  it('requires a fresh explicit rights declaration for an available but changed source version', async () => {
    const f = fixture()
    const first = render(<WritingQuestionAuthorizationPanel child={child} diagnosticItemId={item} request={f.request} />)
    await fill(); first.unmount()
    f.set({ ...required(), revision: 1, status: 'invalidated', source_version: 'source-v2', reason: 'WRITING_QUESTION_AUTHORIZATION_VERSION_CHANGED' })
    render(<WritingQuestionAuthorizationPanel child={child} diagnosticItemId={item} request={f.request} />)
    await screen.findByText(/原授权依据版本已变化/)
    expect(screen.getByRole('checkbox')).not.toBeChecked()
    expect(screen.getByRole('button', { name: '确认用途授权' })).toBeDisabled()
    expect(postCalls(f)).toHaveLength(0)
    await fill()
    fireEvent.click(screen.getByRole('button', { name: '确认用途授权' }))
    await screen.findByText('当前已授权指定用途')
    expect(JSON.parse(String(postCalls(f)[0][1]?.body))).toMatchObject({ expected_revision: 1, source_version: 'source-v2' })
  })

  it.each([401, 403])('requires trusted parent unlock after %s, without submitting', async status => {
    const request = vi.fn<CurriculumParentRequest>().mockRejectedValue(new ApiError('expired', status))
    const unlock = vi.fn()
    render(<WritingQuestionAuthorizationPanel child={child} diagnosticItemId={item} request={request} onRequireParentAuth={unlock} />)
    fireEvent.click(await screen.findByRole('button', { name: '重新解锁家长操作' }))
    expect(unlock).toHaveBeenCalledOnce()
    expect(request.mock.calls.every(([, init]) => !init?.method)).toBe(true)
    expect(screen.queryByRole('button', { name: '确认用途授权' })).not.toBeInTheDocument()
  })

  it('fences late child responses and never displays or writes another child permission state', async () => {
    let release!: (v: unknown) => void
    const delayed = new Promise<unknown>(resolve => { release = resolve })
    const request = vi.fn<CurriculumParentRequest>((path) => path.includes('/child-a/') ? delayed : Promise.resolve(required(other.child_id)))
    const view = render(<WritingQuestionAuthorizationPanel child={child} diagnosticItemId={item} request={request} />)
    view.rerender(<WritingQuestionAuthorizationPanel child={other} diagnosticItemId={item} request={request} />)
    await screen.findByText(required(other.child_id).preview!.question)
    await act(async () => release({ ...required(), status: 'authorized', authorization_id: 'event-1', revision: 1 }))
    expect(screen.queryByText('当前已授权指定用途')).not.toBeInTheDocument()
    expect(screen.queryByText(required().preview!.question)).not.toBeInTheDocument()
    expect(localStorage.getItem(key)).toBeNull()
  })

  it('clears only the intended child and fences a late write after privacy clearing', async () => {
    const f = fixture(); const implementation = f.request.getMockImplementation()!
    let release!: (v: unknown) => void
    const delayed = new Promise<unknown>(resolve => { release = resolve })
    f.request.mockImplementation((path, init) => init?.method ? delayed : implementation(path, init))
    localStorage.setItem(writingAuthorizationLocalKey(other.child_id, item), 'other retained')
    render(<WritingQuestionAuthorizationPanel child={child} diagnosticItemId={item} request={f.request} />)
    await fill(); fireEvent.click(screen.getByRole('button', { name: '确认用途授权' }))
    const body = JSON.parse(String(postCalls(f)[0][1]?.body))
    act(() => clearCurriculumSupplyLocalState(child.child_id))
    await screen.findByText('尚未授权此题用途')
    await act(async () => release({ receipt: { event_id: 'e1', revision: body.expected_revision + 1, command: 'grant' },
      current: { ...required(), revision: 1, status: 'authorized', authorization_id: 'e1' } }))
    expect(localStorage.getItem(key)).toBeNull()
    expect(localStorage.getItem(writingAuthorizationLocalKey(other.child_id, item))).toBe('other retained')
    expect(screen.queryByText('当前已授权指定用途')).not.toBeInTheDocument()
    expect(postCalls(f)).toHaveLength(1)
  })

  it('keeps a late successful write scoped to the original child after switching children', async () => {
    const f = fixture(); const implementation = f.request.getMockImplementation()!
    let release!: (v: unknown) => void
    const delayed = new Promise<unknown>(resolve => { release = resolve })
    f.request.mockImplementation((path, init) => init?.method ? delayed : implementation(path, init))
    const view = render(<WritingQuestionAuthorizationPanel child={child} diagnosticItemId={item} request={f.request} />)
    await fill(); fireEvent.click(screen.getByRole('button', { name: '确认用途授权' }))
    const originalJournal = localStorage.getItem(key)
    view.rerender(<WritingQuestionAuthorizationPanel child={other} diagnosticItemId={item} request={f.request} />)
    await screen.findByText(required(other.child_id).preview!.question)
    await act(async () => release({ receipt: { event_id: 'e1', revision: 1, command: 'grant' },
      current: { ...required(), revision: 1, status: 'authorized', authorization_id: 'e1' } }))
    expect(screen.queryByText('当前已授权指定用途')).not.toBeInTheDocument()
    expect(localStorage.getItem(writingAuthorizationLocalKey(other.child_id, item))).toBeNull()
    expect(localStorage.getItem(key)).toBe(originalJournal)
    expect(postCalls(f)).toHaveLength(1)
  })

  it('refuses tampered pending actor data until explicit local discard', async () => {
    const f = fixture(); f.lose()
    const first = render(<WritingQuestionAuthorizationPanel child={child} diagnosticItemId={item} request={f.request} />)
    await fill(); fireEvent.click(screen.getByRole('button', { name: '确认用途授权' }))
    await screen.findByText(/未能核对结果/); first.unmount()
    const saved = JSON.parse(localStorage.getItem(key)!)
    saved.pending.body.actor_id = 'forged-parent'
    localStorage.setItem(key, JSON.stringify(saved))
    render(<WritingQuestionAuthorizationPanel child={child} diagnosticItemId={item} request={f.request} />)
    await screen.findByText(/本机恢复记录损坏/)
    expect(screen.queryByRole('button', { name: '重试原授权请求' })).not.toBeInTheDocument()
    expect(postCalls(f)).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: '放弃本机授权记录并重新核对' }))
    await waitFor(() => expect(localStorage.getItem(key)).toBeNull())
    expect(postCalls(f)).toHaveLength(1)
  })

  it('rejects wrong-child read and old receipt with a mismatched current identity', async () => {
    const request = vi.fn<CurriculumParentRequest>().mockResolvedValue(required(other.child_id))
    const api = createWritingAuthorizationApi(request)
    await expect(api.read(child.child_id, item)).rejects.toThrow('无法核对')
    const command: WritingAuthorizationCommand = { child_id: child.child_id, diagnostic_item_id: item,
      kind: 'revoke', body: { expected_revision: 1, client_action_id: 'same' } }
    request.mockResolvedValue({ receipt: { event_id: 'event-2', revision: 2, command: 'revoke' }, current: required(other.child_id) })
    await expect(api.perform(child.child_id, item, command)).rejects.toThrow('无法核对')
    request.mockClear()
    await expect(api.perform(other.child_id, item, command)).rejects.toThrow('不属于')
    expect(request).not.toHaveBeenCalled()
  })

  it('does not pass injected actor fields or unknown operations to the trusted adapter', async () => {
    const request = vi.fn<CurriculumParentRequest>()
    const api = createWritingAuthorizationApi(request)
    const command: WritingAuthorizationCommand = { child_id: child.child_id, diagnostic_item_id: item,
      kind: 'revoke', body: { expected_revision: 1, client_action_id: 'same' } }
    await expect(api.perform(child.child_id, item, { ...command,
      body: { ...command.body, actor_id: 'untrusted' } } as WritingAuthorizationCommand)).rejects.toThrow('无法核对')
    await expect(api.perform(child.child_id, item, { ...command, kind: '../grant' } as unknown as WritingAuthorizationCommand)).rejects.toThrow('无法核对')
    expect(request).not.toHaveBeenCalled()
  })

  it('restores a locally saved declaration after remount without treating it as permission or sending a command', async () => {
    const f = fixture()
    const first = render(<WritingQuestionAuthorizationPanel child={child} diagnosticItemId={item} request={f.request} />)
    await fill(); first.unmount()
    render(<WritingQuestionAuthorizationPanel child={child} diagnosticItemId={item} request={f.request} />)
    await screen.findByText('尚未授权此题用途')
    expect(screen.getByLabelText('权利依据说明（至少20字）')).toHaveValue(evidence)
    expect(screen.getByRole('checkbox')).toBeChecked()
    expect(screen.queryByText('当前已授权指定用途')).not.toBeInTheDocument()
    expect(postCalls(f)).toHaveLength(0)
    act(() => clearCurriculumSupplyLocalState())
    await waitFor(() => expect(screen.getByLabelText('权利依据说明（至少20字）')).toHaveValue(''))
    expect(localStorage.getItem(key)).toBeNull()
    expect(postCalls(f)).toHaveLength(0)
  })

  it('clears exact item or all curriculum/authorization state but never unrelated journals', () => {
    const keys = [key, writingAuthorizationLocalKey(child.child_id, 'item:b'), writingAuthorizationLocalKey('child-a:extra', item),
      writingAuthorizationLocalKey(other.child_id, item), 'hsc-curriculum-review:child-a', 'hsc-curriculum-review:child-b', 'unrelated']
    keys.forEach(k => localStorage.setItem(k, 'saved'))
    clearWritingAuthorizationLocalState(child.child_id, item)
    expect(localStorage.getItem(key)).toBeNull()
    expect(localStorage.getItem(keys[1])).toBe('saved')
    clearCurriculumSupplyLocalState(child.child_id)
    expect(localStorage.getItem(keys[1])).toBeNull()
    expect(localStorage.getItem(keys[2])).toBe('saved')
    expect(localStorage.getItem(keys[3])).toBe('saved')
    clearCurriculumSupplyLocalState()
    expect(Object.keys(localStorage)).toEqual(['unrelated'])
  })
})
