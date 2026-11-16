import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ bootstrap: vi.fn(), installationSetupStatus: vi.fn().mockResolvedValue(null), sessionWorkspace: vi.fn(), startSession: vi.fn() }))
vi.mock('./api', () => ({ createParentDraftRecoveryPort: () => undefined, localApi: api, resetRuntimeCache: vi.fn(), ApiError: class extends Error {} }))
vi.mock('./LocalInputs', () => ({ HouseholdStandbyVoiceInput: () => null }))
vi.mock('./DeviceAvailabilityStrip', () => ({ DeviceAvailabilityStrip: () => null }))
vi.mock('./SessionWorkspace', () => ({ SessionWorkspace: ({ initial }: { initial: { marker: string } }) => <div>原学习：{initial.marker}</div> }))
vi.mock('./ParentCenter', () => ({ ParentCenter: ({ onClose }: { onClose: () => void }) => <div>家长 PIN 门禁<button onClick={onClose}>返回原学习</button></div> }))
import { App } from './App'

const child = { child_id: 'primary', nickname: '同学', grade: '四年级', status: 'active' }
const session = { session_id: 'original-session', child_id: child.child_id, status: 'active', state: 'study' }
const workspace = { session, marker: '原任务与待答', plan_items: [] }
let open: (() => void) | undefined
let unsubscribe: ReturnType<typeof vi.fn>
beforeEach(() => {
  localStorage.clear(); vi.clearAllMocks(); open = undefined; unsubscribe = vi.fn()
  api.bootstrap.mockResolvedValue({ children: [child], active_session: session, devices: [], model: { configured: false } })
  api.sessionWorkspace.mockResolvedValue(workspace)
  window.desktop = { onOpenParentCenter: (callback: () => void) => { open = callback; return unsubscribe } } as unknown as Window['desktop']
})
afterEach(() => { cleanup(); delete window.desktop })
async function ready() { render(<App />); await screen.findByText('原学习：原任务与待答'); expect(open).toBeTypeOf('function') }

it('opens the PIN boundary after active session entry and returns through an exact readonly workspace read', async () => {
  await ready()
  act(() => open!())
  expect(screen.getByText('家长 PIN 门禁')).toBeInTheDocument()
  expect(screen.queryByText('原学习：原任务与待答')).not.toBeInTheDocument()
  expect(api.sessionWorkspace).toHaveBeenCalledTimes(1)
  api.sessionWorkspace.mockResolvedValue({ ...workspace, marker: '原任务最新待答' })
  fireEvent.click(screen.getByText('返回原学习'))
  await screen.findByText('原学习：原任务最新待答')
  expect(api.sessionWorkspace).toHaveBeenLastCalledWith(session.session_id)
  expect(api.startSession).not.toHaveBeenCalled()
})

it('does not restore a late close read after a new menu request and does not duplicate start', async () => {
  await ready(); act(() => open!())
  let finish!: (value: unknown) => void
  api.sessionWorkspace.mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
  fireEvent.click(screen.getByText('返回原学习'))
  await waitFor(() => expect(api.sessionWorkspace).toHaveBeenCalledTimes(2))
  act(() => open!())
  await act(async () => { finish(workspace) })
  expect(screen.getByText('家长 PIN 门禁')).toBeInTheDocument()
  expect(screen.queryByText('原学习：原任务与待答')).not.toBeInTheDocument()
  expect(api.startSession).not.toHaveBeenCalled()
})

it.each(['wrong-child', 'unavailable'])('retains the parent boundary when the original return is %s', async (mode) => {
  await ready(); act(() => open!())
  if (mode === 'wrong-child') api.sessionWorkspace.mockResolvedValueOnce({ ...workspace, session: { ...session, child_id: 'other' } })
  else api.sessionWorkspace.mockRejectedValueOnce(new Error('unavailable'))
  fireEvent.click(screen.getByText('返回原学习'))
  await screen.findByRole('alert')
  expect(screen.getByText('家长 PIN 门禁')).toBeInTheDocument()
  expect(screen.queryByText('原学习：原任务与待答')).not.toBeInTheDocument()
  api.sessionWorkspace.mockResolvedValueOnce(workspace)
  fireEvent.click(screen.getByText('重试返回原学习'))
  await screen.findByText('原学习：原任务与待答')
  expect(api.startSession).not.toHaveBeenCalled()
})

it('unsubscribes the native entry listener on App teardown', async () => {
  await ready(); cleanup(); expect(unsubscribe).toHaveBeenCalledTimes(1)
})

it('keeps an early native request through bootstrap without automatically entering the old session first', async () => {
  let finish!: (value: unknown) => void
  api.bootstrap.mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
  render(<App />)
  expect(open).toBeTypeOf('function')
  act(() => open!())
  await act(async () => { finish({ children: [child], active_session: session, devices: [], model: { configured: false } }) })
  await screen.findByText('家长 PIN 门禁')
  expect(api.sessionWorkspace).not.toHaveBeenCalled()
  expect(api.startSession).not.toHaveBeenCalled()
})

it('keeps the current parent entry when a draft synchronously vetoes return, and never queues that return', async () => {
  await ready(); act(() => open!())
  const veto = (event: Event): void => event.preventDefault()
  window.addEventListener('hsc:parent-draft-before-navigation', veto)
  try {
    fireEvent.click(screen.getByText('返回原学习'))
    expect(screen.getByText('家长 PIN 门禁')).toBeInTheDocument()
    expect(api.sessionWorkspace).toHaveBeenCalledTimes(1)
  } finally { window.removeEventListener('hsc:parent-draft-before-navigation', veto) }
  expect(screen.getByText('家长 PIN 门禁')).toBeInTheDocument()
  fireEvent.click(screen.getByText('返回原学习'))
  await screen.findByText('原学习：原任务与待答')
  expect(api.sessionWorkspace).toHaveBeenCalledTimes(2)
  expect(api.startSession).not.toHaveBeenCalled()
})
