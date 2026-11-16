import { StrictMode } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { CompanionMemoryPanel, type Memory } from './CompanionMemoryPanel'

const memory: Memory = {
  memory_id: 'memory-a', content: '喜欢先自己试一试数学题', kind: 'preference',
  source_turn_id: 'turn-private-id', confidence: 0.85,
  created_at: '2026-09-04T10:00:00Z', status: 'active', source_text: '先让我自己想一想。',
}
const result = (items: Memory[] = [memory]) => ({ items, cloud_transmission_allowed: false as const })
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
function search(query: string) {
  fireEvent.change(screen.getByRole('searchbox', { name: '搜索记忆' }), { target: { value: query } })
  fireEvent.submit(screen.getByRole('search', { name: '搜索当前孩子的记忆' }))
}
async function confirmForget() {
  fireEvent.click(await screen.findByRole('button', { name: '忘记这条记忆' }))
  fireEvent.click(screen.getByRole('button', { name: '确认忘记' }))
}

afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('CompanionMemoryPanel', () => {
  it('loads the selected child through props and presents readable source details without raw IDs or JSON', async () => {
    const pending = deferred<ReturnType<typeof result>>()
    const load = vi.fn().mockReturnValue(pending.promise)
    render(<CompanionMemoryPanel childId="child-a" load={load} forget={vi.fn()} />)
    expect(screen.getByRole('status')).toHaveTextContent('正在读取记忆')
    expect(load).toHaveBeenCalledWith('')
    await act(async () => pending.resolve(result()))
    expect(screen.getByText(memory.content)).toBeInTheDocument()
    expect(screen.getByText('偏好 · 生效中')).toBeInTheDocument()
    expect(screen.getByText('记忆不允许传往云端。')).toBeInTheDocument()
    const summary = screen.getByText('来源详情')
    fireEvent.click(summary)
    expect(summary.closest('details')).toHaveAttribute('open')
    expect(screen.getByText(memory.source_text!)).toBeVisible()
    expect(screen.getByText('85%（不代表已核实）')).toBeVisible()
    expect(screen.getByText('陪伴对话')).toBeVisible()
    expect(document.body.textContent).not.toContain('turn-private-id')
    expect(document.body.textContent).not.toContain('source_turn_id')
  })

  it('searches with trimmed text, distinguishes no matches, and clears back to all memories', async () => {
    const load = vi.fn().mockResolvedValueOnce(result()).mockResolvedValueOnce(result([])).mockResolvedValue(result())
    render(<CompanionMemoryPanel childId="child-a" load={load} forget={vi.fn()} />)
    await screen.findByText(memory.content)
    search('  阅读  ')
    expect(await screen.findByText('没有找到相关记忆。')).toBeInTheDocument()
    expect(load).toHaveBeenLastCalledWith('阅读')
    fireEvent.click(screen.getByRole('button', { name: '清空搜索' }))
    expect(await screen.findByText(memory.content)).toBeInTheDocument()
    expect(load).toHaveBeenLastCalledWith('')
    expect(screen.getByRole('searchbox')).toHaveValue('')
  })

  it('does not read without a selected child and explains an empty collection', async () => {
    const load = vi.fn().mockResolvedValue(result([]))
    const { rerender } = render(<CompanionMemoryPanel childId="" load={load} forget={vi.fn()} />)
    expect(screen.getByText('请先选择孩子。')).toBeInTheDocument()
    expect(load).not.toHaveBeenCalled()
    rerender(<CompanionMemoryPanel childId="child-a" load={load} forget={vi.fn()} />)
    expect(await screen.findByText('这个孩子还没有长期记忆。')).toBeInTheDocument()
  })

  it('retries a failed search using the submitted query, not an unsubmitted edit', async () => {
    const load = vi.fn().mockResolvedValueOnce(result()).mockRejectedValueOnce(new Error('{"secret":"token"}')).mockResolvedValue(result())
    render(<CompanionMemoryPanel childId="child-a" load={load} forget={vi.fn()} />)
    await screen.findByText(memory.content)
    search('数学')
    expect(await screen.findByRole('alert')).toHaveTextContent('记忆暂时读取失败')
    expect(document.body.textContent).not.toContain('token')
    expect(screen.queryByText('没有找到相关记忆。')).not.toBeInTheDocument()
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: '未提交' } })
    fireEvent.click(screen.getByRole('button', { name: '重试读取' }))
    await screen.findByText(memory.content)
    expect(load).toHaveBeenLastCalledWith('数学')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('requires confirmation, supports cancellation, and removes only after forget resolves', async () => {
    const pending = deferred<void>()
    const forget = vi.fn().mockReturnValue(pending.promise)
    render(<CompanionMemoryPanel childId="child-a" load={vi.fn().mockResolvedValue(result())} forget={forget} />)
    fireEvent.click(await screen.findByRole('button', { name: '忘记这条记忆' }))
    fireEvent.click(screen.getByRole('button', { name: '取消' }))
    expect(forget).not.toHaveBeenCalled()
    await confirmForget()
    expect(forget).toHaveBeenCalledExactlyOnceWith(memory.memory_id)
    expect(screen.getByText(memory.content)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '正在忘记' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '搜索' })).toBeDisabled()
    await act(async () => pending.resolve())
    expect(screen.queryByText(memory.content)).not.toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('已忘记这条记忆。')
  })

  it('keeps memory and source on deletion failure and lets the parent retry', async () => {
    const forget = vi.fn().mockRejectedValueOnce(new Error('private-backend-detail')).mockResolvedValue(undefined)
    render(<CompanionMemoryPanel childId="child-a" load={vi.fn().mockResolvedValue(result())} forget={forget} />)
    await confirmForget()
    expect(await screen.findByRole('alert')).toHaveTextContent('没有忘记成功，原记忆仍保留。请重试。')
    expect(screen.getByText(memory.content)).toBeInTheDocument()
    expect(screen.getByText(memory.source_text!)).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('private-backend-detail')
    fireEvent.click(screen.getByRole('button', { name: '确认忘记' }))
    await waitFor(() => expect(screen.queryByText(memory.content)).not.toBeInTheDocument())
    expect(forget).toHaveBeenCalledTimes(2)
  })

  it.each(['resolve', 'reject'] as const)('ignores late child-A loading %s after switching to child B', async (settle) => {
    const pending = deferred<ReturnType<typeof result>>()
    const other = { ...memory, content: '另一个孩子喜欢阅读' }
    const { rerender } = render(<CompanionMemoryPanel childId="child-a" load={vi.fn().mockReturnValue(pending.promise)} forget={vi.fn()} />)
    rerender(<CompanionMemoryPanel childId="child-b" load={vi.fn().mockResolvedValue(result([other]))} forget={vi.fn()} />)
    await screen.findByText(other.content)
    await act(async () => { if (settle === 'resolve') pending.resolve(result()); else pending.reject(new Error('旧请求失败')) })
    expect(screen.getByText(other.content)).toBeInTheDocument()
    expect(screen.queryByText(memory.content)).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it.each(['resolve', 'reject'] as const)('isolates pending forget %s and resets search and source details on child switch', async (settle) => {
    const pending = deferred<void>()
    const other = { ...memory, content: '另一个孩子的记忆', source_text: '另一个孩子的原话' }
    const { rerender } = render(<CompanionMemoryPanel childId="child-a" load={vi.fn().mockResolvedValue(result())} forget={vi.fn().mockReturnValue(pending.promise)} />)
    await screen.findByText(memory.content)
    search('数学')
    await screen.findByText(memory.content)
    fireEvent.click(screen.getByText('来源详情'))
    await confirmForget()
    const loadB = vi.fn().mockResolvedValue(result([other]))
    rerender(<CompanionMemoryPanel childId="child-b" load={loadB} forget={vi.fn()} />)
    expect(screen.queryByText(memory.content)).not.toBeInTheDocument()
    await screen.findByText(other.content)
    expect(screen.getByRole('searchbox')).toHaveValue('')
    expect(screen.getByText('来源详情').closest('details')).not.toHaveAttribute('open')
    expect(loadB).toHaveBeenCalledWith('')
    await act(async () => { if (settle === 'resolve') pending.resolve(); else pending.reject(new Error('旧删除失败')) })
    expect(screen.getByText(other.content)).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByText('已忘记这条记忆。')).not.toBeInTheDocument()
  })

  it.each(['resolve', 'reject'] as const)('ignores out-of-order search %s for the same child', async (settle) => {
    const pending = deferred<ReturnType<typeof result>>()
    const latest = { ...memory, content: '最新搜索结果' }
    const load = vi.fn().mockResolvedValueOnce(result()).mockReturnValueOnce(pending.promise).mockResolvedValueOnce(result([latest]))
    render(<CompanionMemoryPanel childId="child-a" load={load} forget={vi.fn()} />)
    await screen.findByText(memory.content)
    search('旧搜索')
    search('新搜索')
    await screen.findByText(latest.content)
    await act(async () => { if (settle === 'resolve') pending.resolve(result()); else pending.reject(new Error('旧搜索失败')) })
    expect(screen.getByText(latest.content)).toBeInTheDocument()
    expect(screen.queryByText(memory.content)).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('uses the latest callback without reloading on every parent render', async () => {
    const original = vi.fn().mockResolvedValue(result())
    const latest = vi.fn().mockResolvedValue(result([]))
    const { rerender } = render(<CompanionMemoryPanel childId="child-a" load={original} forget={vi.fn()} />)
    await screen.findByText(memory.content)
    rerender(<CompanionMemoryPanel childId="child-a" load={latest} forget={vi.fn()} />)
    expect(latest).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '刷新记忆' }))
    await screen.findByText('这个孩子还没有长期记忆。')
    expect(latest).toHaveBeenCalledExactlyOnceWith('')
  })

  it('has readable fallbacks for missing sources and unknown metadata', async () => {
    const item = { ...memory, source_text: undefined, source_turn_id: null, confidence: NaN, created_at: 'bad-date', kind: 'internal_kind', status: 'internal_status' }
    render(<CompanionMemoryPanel childId="child-a" load={vi.fn().mockResolvedValue(result([item]))} forget={vi.fn()} />)
    await screen.findByText(memory.content)
    fireEvent.click(screen.getByText('来源详情'))
    expect(screen.getByText('其他记忆 · 状态待确认')).toBeInTheDocument()
    expect(screen.getByText('来源暂未提供')).toBeVisible()
    expect(screen.getByText('原始对话暂未提供。')).toBeVisible()
    expect(screen.getByText('时间暂未提供')).toBeVisible()
    expect(screen.getByText('置信度暂未提供')).toBeVisible()
  })

  it('does not let a superseded StrictMode mount replace current results', async () => {
    const pending = deferred<ReturnType<typeof result>>()
    const load = vi.fn().mockReturnValueOnce(pending.promise).mockResolvedValue(result([]))
    render(<StrictMode><CompanionMemoryPanel childId="child-a" load={load} forget={vi.fn()} /></StrictMode>)
    await screen.findByText('这个孩子还没有长期记忆。')
    await act(async () => pending.resolve(result()))
    expect(screen.queryByText(memory.content)).not.toBeInTheDocument()
  })

  it('forgets only the selected row', async () => {
    const other = { ...memory, memory_id: 'memory-b', content: '另一条记忆' }
    const forget = vi.fn().mockResolvedValue(undefined)
    render(<CompanionMemoryPanel childId="child-a" load={vi.fn().mockResolvedValue(result([memory, other]))} forget={forget} />)
    const article = (await screen.findByText(memory.content)).closest('article')!
    fireEvent.click(within(article).getByRole('button', { name: '忘记这条记忆' }))
    fireEvent.click(within(article).getByRole('button', { name: '确认忘记' }))
    await waitFor(() => expect(screen.queryByText(memory.content)).not.toBeInTheDocument())
    expect(screen.getByText(other.content)).toBeInTheDocument()
  })
})
