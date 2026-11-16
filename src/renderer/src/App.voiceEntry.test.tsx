import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const doubles = vi.hoisted(() => ({ onWake: null as null | ((profile: { childId: string; wakeWord: string; configured: boolean }, command: string) => Promise<void>), bootstrap: vi.fn(), installationSetupStatus: vi.fn().mockResolvedValue(null), startSession: vi.fn(), sessionWorkspace: vi.fn() }))
vi.mock('./api', () => ({ createParentDraftRecoveryPort: () => undefined, localApi: doubles, resetRuntimeCache: vi.fn(), requestParentLocal: vi.fn(), ApiError: class extends Error {} }))
vi.mock('./LocalInputs', () => ({ HouseholdStandbyVoiceInput: (props: { onWake: typeof doubles.onWake }) => { doubles.onWake = props.onWake; return null } }))
vi.mock('./SessionWorkspace', () => ({ SessionWorkspace: () => <div>真实身份已选择，等待初始化</div> }))
vi.mock('./DeviceAvailabilityStrip', () => ({ DeviceAvailabilityStrip: () => null }))
import { App } from './App'

describe('spoken first-entry identity', () => {
  afterEach(() => { cleanup(); delete window.desktop; vi.restoreAllMocks() })
  it('uses an unconfigured nickname only to select the child, never silently as their chosen wake word', async () => {
    const child = { child_id: 'child-4', nickname: '小语', grade: '四年级', status: 'active' }
    doubles.bootstrap.mockResolvedValue({ children: [child], active_session: null, devices: [], model: { configured: false }, privacy_paused: false })
    doubles.startSession.mockResolvedValue({ session_id: 'session-1', child_id: child.child_id })
    doubles.sessionWorkspace.mockResolvedValue({ session: { session_id: 'session-1', child_id: child.child_id } })
    const saveWakeWordSettings = vi.fn()
    window.desktop = { getWakeWordSettings: vi.fn().mockResolvedValue(null), saveWakeWordSettings } as never
    render(<App />)
    await screen.findByText('小语')
    await waitFor(() => expect(doubles.onWake).not.toBeNull())
    await act(async () => { await doubles.onWake!({ childId: child.child_id, wakeWord: child.nickname, configured: false }, '') })
    expect(await screen.findByText('真实身份已选择，等待初始化')).toBeInTheDocument()
    expect(doubles.startSession).toHaveBeenCalledWith(child.child_id)
    expect(saveWakeWordSettings).not.toHaveBeenCalled()
  })
})
