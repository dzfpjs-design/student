import { fireEvent, render, screen, waitFor, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './api'
import { ManualStandardScopePanel } from './ManualStandardScopePanel'

const child = { child_id: 'child-primary-4', grade: '四年级' }
const base = `/v1/parent/children/${child.child_id}/manual-standard-requirements`
const source = { source_id: 'public-official', title: '官方数学课标', version_label: '2022年版', official_url: 'https://www.moe.gov.cn/', pdf_sha256: 'a'.repeat(64), page_count: 189, status: 'downloaded' }
const node = { knowledge_node_id: 'synthetic-manual-skill', title: '隔离合成运算技能', description: '只用于界面流程验证', source_ref: 'isolated:manual', knowledge_version: 'v1' }
const context = { child_id: child.child_id, grade: child.grade, stage_scope: '第二学段', sources: [source], nodes: [node] }
const quote = '理解四则运算的意义，能进行整数四则混合运算。'
const image = 'data:image/png;base64,cHVibGlj'
function packet(state = 'draft', missingNode = false) {
  return { candidate_id: 'saved-manual', child_id: child.child_id, version: state === 'draft' ? 1 : state === 'approved' ? 2 : 3,
    state, eligible: state === 'approved', blockers: [], source, scope: { node: missingNode ? null : node },
    payload: { title: '整数运算要求', quote, observable_outcome: '能说明运算步骤和问题中的数量关系。' },
    pages: [{ pdf_page: 28, image_sha256: 'b'.repeat(64), image_data_url: image }, { pdf_page: 27, image_sha256: 'c'.repeat(64), image_data_url: image }],
    events: [{ action_id: 'initial-draft', kind: 'draft', version: 1, note: null as string | null, occurred_at: '2026-10-13T00:00:00Z' }] }
}
function fixture(initial: ReturnType<typeof packet> | null = packet()) {
  let current = initial
  const api = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/context')) return context
    if (path === base && !init?.method) return current ? [{ candidate_id: current.candidate_id, title: current.payload.title, version: current.version, state: current.state, page_number: 28 }] : []
    if (init?.method === 'POST') {
      const body = JSON.parse(String(init.body)); const kind = path.endsWith('/withdraw') ? 'withdraw' : path.endsWith('/review') ? body.decision : 'draft'
      current = packet(kind === 'approve' ? 'approved' : kind === 'withdraw' ? 'withdrawn' : kind === 'reject' ? 'rejected' : 'draft')
      current.events.push({ action_id: body.action_id, kind, version: current.version, note: body.note ?? null, occurred_at: '2026-10-13T00:01:00Z' })
      return current
    }
    if (path.includes('/actions/')) {
      const action = path.split('/').at(-1)
      if (current?.events.some(e => e.action_id === action)) return current
      throw new ApiError('未保存', 404, 'MANUAL_ACTION_NOT_SAVED')
    }
    if (path.includes('/saved-manual')) return current
    throw new Error('unsupported fixture path')
  })
  return { api, current: () => current }
}
beforeEach(() => { localStorage.clear() })
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('parent manual official requirement journey', () => {
  it('shows both exact stored pages beside explicit approval and saves withdrawal without hiding history', async () => {
    const f = fixture(); render(<ManualStandardScopePanel child={child} api={f.api} />)
    await screen.findByAltText('已保存的官方数学课标 PDF 第 28 页')
    expect(screen.getAllByRole('img')).toHaveLength(2)
    const approval = screen.getByRole('button', { name: '确认审核这条要求' })
    expect(approval).toBeDisabled()
    fireEvent.change(screen.getByLabelText('审核或撤销说明'), { target: { value: '已核对原页、第二学段和准确节点。' } })
    expect(approval).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(approval)
    await screen.findByRole('button', { name: '撤销这条审核' })
    const approveCall = f.api.mock.calls.find(([, init]) => init?.method === 'POST')!
    expect(JSON.parse(String(approveCall[1]!.body))).toMatchObject({ expected_version: 1, decision: 'approve' })
    fireEvent.change(screen.getByLabelText('审核或撤销说明'), { target: { value: '撤销当前授权，保留历史事实。' } })
    fireEvent.click(screen.getByRole('button', { name: '撤销这条审核' }))
    await waitFor(() => expect(f.current()?.state).toBe('withdrawn'))
    await waitFor(() => expect(screen.queryByRole('button', { name: '撤销这条审核' })).not.toBeInTheDocument())
    expect(screen.getAllByRole('img')).toHaveLength(2)
    expect(f.current()?.events.at(-1)?.kind).toBe('withdraw')
  })

  it('does not approve a real draft with missing current school node or missing original page', async () => {
    const f = fixture(packet('draft', true)); render(<ManualStandardScopePanel child={child} api={f.api} />)
    await screen.findByText('学校知识点：尚未绑定，不能审核通过')
    expect(screen.getByRole('checkbox')).toBeDisabled()
    expect(screen.getByRole('button', { name: '确认审核这条要求' })).toBeDisabled()
    expect(f.api.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false)
  })

  it('saves an unapproved draft using registered source hash and the selected exact school node', async () => {
    const f = fixture(null); render(<ManualStandardScopePanel child={child} api={f.api} />)
    await screen.findByText('尚无逐条登记记录。')
    const fill = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } })
    fill('官方课标来源', source.source_id); fill('当前学校知识点', node.knowledge_node_id)
    fill('要求所在 PDF 页码', '28'); fill('学段说明所在 PDF 页码', '27')
    fill('要求所在页的人工摘录', quote); fill('学段说明页的人工摘录', '第二学段（3～4年级）')
    fill('本条要求的准确原句', quote); fill('要求标题', '整数运算要求'); fill('可观察的学习结果', '能说明问题中的数量关系和运算过程。')
    fireEvent.click(screen.getByRole('button', { name: '保存草稿并读取原页' }))
    await screen.findByAltText('已保存的官方数学课标 PDF 第 28 页')
    const body = JSON.parse(String(f.api.mock.calls.find(([, init]) => init?.method === 'POST')![1]!.body))
    expect(body).toMatchObject({ source_pdf_sha256: source.pdf_sha256, source_version: source.version_label, expected_version: 0,
      knowledge_node_id: node.knowledge_node_id, page_number: 28, stage_page_number: 27 })
    expect(body).not.toHaveProperty('image_sha256'); expect(f.current()?.state).toBe('draft')
  })

  it('recovers a lost mutation response by original action lookup after remount, without a second POST', async () => {
    const f = fixture(); const original = f.api.getMockImplementation()!
    let lost = true
    f.api.mockImplementation(async (path, init) => {
      const result = await original(path, init)
      if (init?.method === 'POST' && lost) { lost = false; throw new ApiError('断线', undefined, 'LOCAL_SERVICE_OFFLINE') }
      return result
    })
    const first = render(<ManualStandardScopePanel child={child} api={f.api} />)
    await screen.findByRole('checkbox'); fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.change(screen.getByLabelText('审核或撤销说明'), { target: { value: '逐页核对后提交。' } })
    fireEvent.click(screen.getByRole('button', { name: '确认审核这条要求' }))
    await screen.findByRole('button', { name: '重试原请求' })
    first.unmount(); render(<ManualStandardScopePanel child={child} api={f.api} />)
    await screen.findByText('已从本机记录找回保存结果。')
    expect(f.api.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
    expect(screen.queryByRole('button', { name: '重试原请求' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '撤销这条审核' })).toBeInTheDocument()
  })

  it('rejects local storage failure before sending a decision, and hides primary-grade workflow for junior child', async () => {
    const f = fixture(); const view = render(<ManualStandardScopePanel child={child} api={f.api} />)
    await screen.findByRole('checkbox'); fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.change(screen.getByLabelText('审核或撤销说明'), { target: { value: '明确审核说明。' } })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('disk full') })
    fireEvent.click(screen.getByRole('button', { name: '确认审核这条要求' }))
    await screen.findByText('本机无法保存草稿或原请求，尚未发送新操作。恢复存储后重试。')
    expect(f.api.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false)
    vi.restoreAllMocks()
    fireEvent.click(screen.getByRole('button', { name: '重新读取本机草稿' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: '重新读取本机草稿' })).not.toBeInTheDocument())
    expect(screen.getByRole('button', { name: '确认审核这条要求' })).toBeDisabled()
    expect(f.api.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false)
    view.unmount(); render(<ManualStandardScopePanel child={{ child_id: 'child-junior-3', grade: '初三' }} api={f.api} />)
    expect(screen.queryByLabelText('人工逐条课标登记')).not.toBeInTheDocument()
  })
})


it('pauses new decisions while privacy recovery is active', async () => {
  const f = fixture()
  render(<ManualStandardScopePanel child={child} api={f.api} />)
  await screen.findByRole('checkbox')
  window.dispatchEvent(new CustomEvent('hsc-privacy-pause'))
  await screen.findByText('数据恢复或删除仍在核对中，请完成后重新打开本页。')
  expect(screen.queryByRole('button', { name: '确认审核这条要求' })).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: '刷新已保存记录' })).toBeDisabled()
  expect(f.api.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false)
})


it('ignores a late approval response after privacy pause and retains the original pending action', async () => {
  const f = fixture(); const original = f.api.getMockImplementation()!
  let release: ((value: Awaited<ReturnType<typeof original>>) => void) | null = null
  f.api.mockImplementation(async (path, init) => {
    const result = await original(path, init)
    if (init?.method === 'POST') return new Promise(resolve => { release = resolve })
    return result
  })
  render(<ManualStandardScopePanel child={child} api={f.api} />)
  await screen.findByRole('checkbox'); fireEvent.click(screen.getByRole('checkbox'))
  fireEvent.change(screen.getByLabelText('审核或撤销说明'), { target: { value: '明确旧文档审核说明。' } })
  fireEvent.click(screen.getByRole('button', { name: '确认审核这条要求' }))
  await waitFor(() => expect(release).not.toBeNull())
  window.dispatchEvent(new CustomEvent('hsc-privacy-pause'))
  await screen.findByText('数据恢复或删除仍在核对中，请完成后重新打开本页。')
  release!(f.current())
  await Promise.resolve()
  expect(screen.queryByLabelText('课标原页与审核')).not.toBeInTheDocument()
  expect(JSON.parse(localStorage.getItem('hsc-manual-standard:child-primary-4')!).pending.kind).toBe('approve')
  expect(f.api.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
})
