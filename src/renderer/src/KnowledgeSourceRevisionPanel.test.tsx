import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { KnowledgeSourceRevisionPanel } from './KnowledgeSourceRevisionPanel'
import { ApiError } from './api'
import { clearSourceRevisionLocalState, readSourceRevisionJournal, saveSourceRevisionJournal,
  type SourceRevisionJournal } from './sourceRevisionApi'

const old = { source_id: 'old', child_id: 'one', title: '旧物理原题', review_status: 'approved', content_hash: 'h', current_version: 1, predecessor_id: null }
const next = { ...old, source_id: 'new', review_status: 'quarantined', predecessor_id: 'old' }
const version = 'a'.repeat(64)
const preview = { source: old, source_version: version, status: 'incomplete',
  stored_excerpts: [{ text: '旧保存片段', sequence: 1 }], replacement_text: '完整物理原题', replacement_length: 1721, stored_length: 1000 }
const detail = { current: next, source_version: 'b'.repeat(64), stored_excerpts: [{ text: '完整物理原题', sequence: 1 }], history: [], purpose_permission_inherited: false }
beforeEach(() => localStorage.clear())
afterEach(cleanup)

function requestFixture() {
  let current = { ...next }
  let lose = false
  const writes: Array<{ path: string; body: Record<string, unknown> }> = []
  const request = vi.fn(async (path: string, init?: RequestInit): Promise<unknown> => {
    if (path.endsWith('/knowledge-sources')) return { items: [old, ...(writes.length ? [current] : [])] }
    if (path.endsWith('/preview')) return preview
    if (init?.method === 'POST') {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>
      writes.push({ path, body })
      if (path.endsWith('/review')) current = { ...current, review_status: 'approved' }
      if (lose) { lose = false; throw new Error('回执连接中断') }
      return { current, source_version: detail.source_version }
    }
    return { ...detail, current }
  })
  return { request: request as unknown as NonNullable<Parameters<typeof KnowledgeSourceRevisionPanel>[0]['request']>,
    writes, loseResponse: () => { lose = true } }
}
async function compare() {
  await screen.findByRole('option', { name: /旧物理原题/ })
  fireEvent.change(screen.getByRole('combobox'), { target: { value: 'old' } })
  fireEvent.change(screen.getByLabelText('完整原文'), { target: { value: '完整物理原题' } })
  fireEvent.click(screen.getByRole('button', { name: '仅核对，不保存版本' }))
  await screen.findByText('保存片段与完整原文不一致')
}

describe('explicit source revision parent workflow', () => {
  it('preview/cancel never writes; revise is distinct from review and cannot inherit permission', async () => {
    const api = requestFixture()
    render(<KnowledgeSourceRevisionPanel childId="one" request={api.request} />)
    await compare()
    expect(api.writes).toHaveLength(0)
    fireEvent.click(screen.getByRole('button', { name: '取消，本次不写入' }))
    expect(api.writes).toHaveLength(0)
    fireEvent.click(screen.getByRole('button', { name: '仅核对，不保存版本' }))
    await screen.findByText('保存片段与完整原文不一致')
    expect(screen.getByRole('button', { name: '确认创建新版本' })).toBeDisabled()
    fireEvent.click(screen.getByLabelText('我确认保留旧稿，创建独立待审核新版本'))
    fireEvent.click(screen.getByRole('button', { name: '确认创建新版本' }))
    await screen.findByText(/新版本已保存/)
    expect(api.writes).toHaveLength(1)
    expect(api.writes[0].body).not.toHaveProperty('actor')
    fireEvent.click(screen.getByRole('button', { name: '读取当前资料与历史' }))
    await screen.findByText(/旧物理原题 · 新版待审核/)
    expect(screen.getByRole('button', { name: '保存新版来源审核' })).toBeDisabled()
    fireEvent.click(screen.getByLabelText(/我已重新核对新版完整内容与来源/))
    fireEvent.click(screen.getByRole('button', { name: '保存新版来源审核' }))
    await screen.findByText(/具体题目的用途授权仍需重新确认/)
    expect(api.writes[1].body.client_action_id).not.toBe(api.writes[0].body.client_action_id)
  })

  it('lost response remount retains exact pending key, never auto sends, then reads current', async () => {
    const api = requestFixture()
    const first = render(<KnowledgeSourceRevisionPanel childId="one" request={api.request} />)
    await compare()
    api.loseResponse()
    fireEvent.click(screen.getByLabelText('我确认保留旧稿，创建独立待审核新版本'))
    fireEvent.click(screen.getByRole('button', { name: '确认创建新版本' }))
    await screen.findByText('回执连接中断')
    const saved = readSourceRevisionJournal('one')!.pending!
    first.unmount()
    render(<KnowledgeSourceRevisionPanel childId="one" request={api.request} />)
    await screen.findByText('原操作尚待核对。不会自动发送或改变原版本。')
    expect(api.writes).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: '用原操作重试并核对回执' }))
    await screen.findByText(/新版本已保存/)
    expect(api.writes[1].body).toEqual(saved.body)
    expect(readSourceRevisionJournal('one')!.pending).toBeNull()
  })

  it('storage failure blocks sends and clear is exact child; restore clears only namespace', async () => {
    const base: SourceRevisionJournal = { schema: 1, childId: 'two', sourceId: 'other', text: '另孩原文', pending: null }
    saveSourceRevisionJournal(base)
    localStorage.setItem('hsc:unrelated', 'keep')
    localStorage.setItem('hsc:source-revision:one', '{broken')
    const api = requestFixture()
    render(<KnowledgeSourceRevisionPanel childId="one" request={api.request} />)
    await screen.findByRole('button', { name: '放弃本机恢复记录，不撤销服务端保存' })
    expect(api.writes).toHaveLength(0)
    fireEvent.click(screen.getByRole('button', { name: '放弃本机恢复记录，不撤销服务端保存' }))
    expect(readSourceRevisionJournal('two')).toEqual(base)
    clearSourceRevisionLocalState()
    expect(readSourceRevisionJournal('two')).toBeNull()
    expect(localStorage.getItem('hsc:unrelated')).toBe('keep')
  })

  it('changed source 409 keeps original operation; delete 404 does not display old preview', async () => {
    const api = requestFixture()
    let reject = false
    const request: typeof api.request = async (path, init) => {
      if (reject) throw new ApiError('版本变化，请核对', init?.method === 'POST' ? 409 : 404)
      return api.request(path, init)
    }
    render(<KnowledgeSourceRevisionPanel childId="one" request={request} />)
    await compare(); reject = true
    fireEvent.click(screen.getByLabelText('我确认保留旧稿，创建独立待审核新版本'))
    fireEvent.click(screen.getByRole('button', { name: '确认创建新版本' }))
    await screen.findByText('版本变化，请核对')
    expect(readSourceRevisionJournal('one')?.pending).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '读取当前资料与历史' }))
    await screen.findByText('资料已删除或不属于当前孩子。没有恢复旧副本。')
    expect(screen.queryByText('保存片段与完整原文不一致')).not.toBeInTheDocument()
  })

  it('late old child response does not change new child or its journal', async () => {
    const api = requestFixture()
    let release!: (value: unknown) => void
    const request = vi.fn(async (path: string, init?: RequestInit) => {
      if (path.endsWith('/preview')) return new Promise<unknown>((resolve) => { release = resolve })
      return api.request(path, init)
    }) as unknown as typeof api.request
    const panel = render(<KnowledgeSourceRevisionPanel childId="one" request={request} />)
    await screen.findByRole('option', { name: /旧物理原题/ })
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'old' } })
    fireEvent.change(screen.getByLabelText('完整原文'), { target: { value: '完整物理原题' } })
    fireEvent.click(screen.getByRole('button', { name: '仅核对，不保存版本' }))
    await waitFor(() => expect(release).toBeTypeOf('function'))
    panel.rerender(<KnowledgeSourceRevisionPanel childId="two" request={request} />)
    release(preview)
    await waitFor(() => expect(screen.getByLabelText('完整原文')).toHaveValue(''))
    expect(screen.queryByText('保存片段与完整原文不一致')).not.toBeInTheDocument()
    expect(readSourceRevisionJournal('two')).toBeNull()
  })

  it('clearing a consumed import preset cannot erase a current read or saved revision', async () => {
    const api = requestFixture()
    saveSourceRevisionJournal({ schema: 1, childId: 'one', sourceId: 'new', text: '', pending: null })
    let release!: (value: unknown) => void
    const request = vi.fn(async (path: string, init?: RequestInit) => {
      if (path.endsWith('/new/revision')) return new Promise<unknown>((resolve) => { release = resolve })
      return api.request(path, init)
    }) as unknown as typeof api.request
    const panel = render(<KnowledgeSourceRevisionPanel childId="one" initialSourceId="new" request={request} />)
    await screen.findByRole('option', { name: /旧物理原题/ })
    fireEvent.click(screen.getByRole('button', { name: '读取当前资料与历史' }))
    await waitFor(() => expect(release).toBeTypeOf('function'))
    panel.rerender(<KnowledgeSourceRevisionPanel childId="one" initialSourceId="" request={request} />)
    release(detail)
    await screen.findByText(/旧物理原题 · 新版待审核/)
    expect(readSourceRevisionJournal('one')!.sourceId).toBe('new')
  })
})
