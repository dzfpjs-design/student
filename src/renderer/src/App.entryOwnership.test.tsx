import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const doubles = vi.hoisted(() => ({ bootstrap: vi.fn(), installationSetupStatus: vi.fn().mockResolvedValue(null), startSession: vi.fn(), sessionWorkspace: vi.fn(),
  onWake: null as null | ((profile: { childId: string; wakeWord: string; configured: boolean }, command: string) => Promise<void>) }))
vi.mock('./api', () => ({ createParentDraftRecoveryPort: () => undefined, localApi: doubles, resetRuntimeCache: vi.fn(), ApiError: class extends Error {} }))
vi.mock('./LocalInputs', () => ({ HouseholdStandbyVoiceInput: (props: { onWake: typeof doubles.onWake }) => { doubles.onWake = props.onWake; return null } }))
vi.mock('./SessionWorkspace', () => ({ SessionWorkspace: () => <div>原学习任务</div> }))
vi.mock('./ParentCenter', () => ({ ParentCenter: ({ onClose }: { onClose: () => void }) => <div>家长操作区<button onClick={onClose}>返回待机</button></div> }))
vi.mock('./DeviceAvailabilityStrip', () => ({ DeviceAvailabilityStrip: () => null }))
import { App } from './App'

const child = { child_id: 'child-4', nickname: '小语', grade: '四年级', curriculum_region: '江苏南京', status: 'active' }
const session = { session_id: 'original-session', child_id: child.child_id }
const state = { children: [child], active_session: session, devices: [], model: { configured: false }, privacy_paused: false }
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

describe('explicit parent entry owns the view over pending automatic resume', () => {
  beforeEach(() => { localStorage.clear(); vi.clearAllMocks(); doubles.bootstrap.mockResolvedValue(state) })
  afterEach(() => { cleanup(); delete window.desktop; vi.restoreAllMocks() })

  it.each(['success', 'failure'] as const)('does not let late %s replace the parent view', async (outcome) => {
    const pending = deferred<{ session: typeof session }>()
    doubles.sessionWorkspace.mockReturnValueOnce(pending.promise)
    render(<App />)
    await waitFor(() => expect(doubles.sessionWorkspace).toHaveBeenCalledWith(session.session_id))
    fireEvent.click(screen.getByRole('button', { name: /家长中心/ }))
    expect(screen.getByText('家长操作区')).toBeInTheDocument()
    await act(async () => {
      if (outcome === 'success') pending.resolve({ session })
      else pending.reject(new Error('late workspace read failed'))
      await pending.promise.catch(() => {})
    })
    expect(screen.getByText('家长操作区')).toBeInTheDocument()
    expect(screen.queryByText('原学习任务')).not.toBeInTheDocument()
    expect(screen.queryByText('暂时无法进入')).not.toBeInTheDocument()
    expect(doubles.startSession).not.toHaveBeenCalled()
  })

  it('keeps the original session and resumes it only on a new explicit voice entry after parent closes', async () => {
    const pending = deferred<{ session: typeof session }>()
    doubles.sessionWorkspace.mockReturnValueOnce(pending.promise).mockResolvedValue({ session })
    render(<App />)
    await waitFor(() => expect(doubles.sessionWorkspace).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByRole('button', { name: /家长中心/ }))
    await act(async () => { pending.resolve({ session }); await pending.promise })
    fireEvent.click(screen.getByRole('button', { name: '返回待机' }))
    await screen.findByText('继续今天的学习')
    expect(doubles.sessionWorkspace).toHaveBeenCalledTimes(1)
    await act(async () => { await doubles.onWake!({ childId: child.child_id, wakeWord: child.nickname, configured: true }, '') })
    expect(screen.getByText('原学习任务')).toBeInTheDocument()
    expect(doubles.sessionWorkspace).toHaveBeenNthCalledWith(2, session.session_id)
    expect(doubles.startSession).not.toHaveBeenCalled()
  })

  it('ignores an already queued standby wake callback after entering the parent view', async () => {
    doubles.bootstrap.mockResolvedValue({ ...state, active_session: null })
    render(<App />)
    await screen.findByRole('button', { name: /家长中心/ })
    const oldWake = doubles.onWake!
    fireEvent.click(screen.getByRole('button', { name: /家长中心/ }))
    await act(async () => { await oldWake({ childId: child.child_id, wakeWord: child.nickname, configured: true }, '开始学习') })
    expect(screen.getByText('家长操作区')).toBeInTheDocument()
    expect(doubles.startSession).not.toHaveBeenCalled()
    expect(doubles.sessionWorkspace).not.toHaveBeenCalled()
  })
})
