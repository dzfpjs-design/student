import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ParentPetAudioTeaching } from './ParentPetAudioTeaching'
import { parentPetPendingPrefix, clearParentPetMaterialPending } from './petMaterialSupplyTypes'

beforeEach(() => localStorage.clear())
afterEach(() => { cleanup(); vi.restoreAllMocks() })

function api() {
  let grant: Record<string, unknown> | null = null
  let loss = false
  let items: unknown[] = []
  const request = vi.fn(async (path: string, init?: RequestInit): Promise<unknown> => {
    if (init?.method === 'POST') {
      const body = JSON.parse(String(init.body))
      grant = { ...body, revision: body.expected_revision + 1 }
      if (loss) throw new Error('synthetic response loss after committed grant')
      return { child_id: 'child-a', grant }
    }
    if (path.includes('/authorization/actions/')) return { child_id: 'child-a', state: grant ? 'applied' : 'missing', grant }
    if (path.endsWith('/authorization')) return { child_id: 'child-a', configured: true, current: grant?.allowed ?? false, grant }
    return { child_id: 'child-a', items }
  })
  return { request: request as unknown as <T>(path: string, init?: RequestInit) => Promise<T>, spy: request,
    lose: () => { loss = true }, work: (value: unknown[]) => { items = value } }
}

describe('parent original-audio purpose', () => {
  it('does not mistake a later work generation for the exact lost retry receipt', async () => {
    const key = parentPetPendingPrefix('child-a') + 'original-audio-purpose'
    localStorage.setItem(key, JSON.stringify({ childId: 'child-a', kind: 'retry', workId: 'work-a',
      body: { action_id: 'old-action', expected_generation: 0, acknowledge_possible_charge: true } }))
    const backend = api()
    const request = vi.fn(async (path: string, init?: RequestInit): Promise<unknown> => {
      if (path.includes('/retry/actions/')) return { child_id: 'child-a', work_id: 'work-a', action_id: 'other-action', state: 'applied', generation: 1 }
      if (path.endsWith('/work/work-a')) return { child_id: 'child-a', work_id: 'work-a', generation: 2 }
      return backend.request(path, init)
    })
    render(<ParentPetAudioTeaching childId="child-a" request={request as <T>(path: string, init?: RequestInit) => Promise<T>} />)
    await screen.findByText('上次操作待核对')
    fireEvent.click(screen.getByText('核对原操作'))
    await screen.findByText('尚未找到原操作。已保持暂停；可放弃本机操作后重新确认。')
    expect(localStorage.getItem(key)).not.toBeNull()
    expect(request.mock.calls.every(([, init]) => init?.method !== 'POST')).toBe(true)
  })

  it('blocks new sends with damaged local intent until explicitly cleared', async () => {
    localStorage.setItem(parentPetPendingPrefix('child-a') + 'original-audio-purpose', '{broken')
    const backend = api()
    render(<ParentPetAudioTeaching childId="child-a" request={backend.request} />)
    await screen.findByText('此用途尚未获得当前许可')
    fireEvent.click(screen.getByLabelText('允许原音练习建议'))
    fireEvent.click(screen.getByText('保存授权'))
    expect(backend.spy.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(0)
    fireEvent.click(screen.getByText('清除本机损坏记录'))
    expect(localStorage.getItem(parentPetPendingPrefix('child-a') + 'original-audio-purpose')).toBeNull()
  })

  it('allows revocation even when unsaved budget fields are invalid', async () => {
    const backend = api()
    render(<ParentPetAudioTeaching childId="child-a" request={backend.request} />)
    await screen.findByText('此用途尚未获得当前许可')
    fireEvent.click(screen.getByLabelText('允许原音练习建议')); fireEvent.click(screen.getByText('保存授权'))
    await screen.findByText('此用途已获许可')
    fireEvent.change(screen.getByLabelText('原音每日预算'), { target: { value: '-3' } })
    fireEvent.click(screen.getByText('撤销用途许可'))
    await screen.findByText('此用途尚未获得当前许可')
    const posts = backend.spy.mock.calls.filter(([, init]) => init?.method === 'POST')
    expect(posts).toHaveLength(2)
    expect(JSON.parse(String(posts[1][1]?.body)).allowed).toBe(false)
  })

  it('loads without granting or dispatching and saves explicit child-scoped permission', async () => {
    const backend = api()
    render(<ParentPetAudioTeaching childId="child-a" request={backend.request} />)
    await screen.findByText('此用途尚未获得当前许可')
    expect(backend.spy.mock.calls.every(([, init]) => init?.method !== 'POST')).toBe(true)
    fireEvent.click(screen.getByLabelText('允许原音练习建议'))
    fireEvent.click(screen.getByText('保存授权'))
    await screen.findByText('此用途已获许可')
    expect(backend.spy.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
    expect(localStorage.getItem(parentPetPendingPrefix('child-a') + 'original-audio-purpose')).toBeNull()
  })

  it('retains response-loss intent across remount and only reads the original action receipt', async () => {
    const backend = api(); backend.lose()
    const first = render(<ParentPetAudioTeaching childId="child-a" request={backend.request} />)
    await screen.findByText('此用途尚未获得当前许可')
    fireEvent.click(screen.getByLabelText('允许原音练习建议')); fireEvent.click(screen.getByText('保存授权'))
    await screen.findByText('上次操作待核对')
    expect(localStorage.getItem(parentPetPendingPrefix('child-a') + 'original-audio-purpose')).not.toBeNull()
    first.unmount()
    render(<ParentPetAudioTeaching childId="child-a" request={backend.request} />)
    await screen.findByText('上次操作待核对')
    fireEvent.click(screen.getByText('核对原操作'))
    await screen.findByText('原操作已找到，没有补发请求。')
    expect(backend.spy.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
  })

  it('cannot post when local persistence fails', async () => {
    const backend = api()
    render(<ParentPetAudioTeaching childId="child-a" request={backend.request} />)
    await screen.findByText('此用途尚未获得当前许可')
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('full') })
    fireEvent.click(screen.getByLabelText('允许原音练习建议')); fireEvent.click(screen.getByText('保存授权'))
    await screen.findByText('本机存储不可用，未发送授权或重试。')
    expect(backend.spy.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(0)
  })

  it('rejects cross-child response and child-scoped existing privacy cleanup removes only its pending intent', async () => {
    const backend = api()
    localStorage.setItem(parentPetPendingPrefix('child-a') + 'original-audio-purpose', 'a')
    localStorage.setItem(parentPetPendingPrefix('child-b') + 'original-audio-purpose', 'b')
    clearParentPetMaterialPending('child-a')
    expect(localStorage.getItem(parentPetPendingPrefix('child-a') + 'original-audio-purpose')).toBeNull()
    expect(localStorage.getItem(parentPetPendingPrefix('child-b') + 'original-audio-purpose')).toBe('b')
    render(<ParentPetAudioTeaching childId="child-b" request={backend.request} />)
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('暂不可用'))
    expect(screen.queryByText('此用途已获许可')).toBeNull()
    expect(backend.spy.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(0)
  })
})
