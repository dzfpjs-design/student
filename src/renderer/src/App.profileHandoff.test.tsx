import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VoiceProfileSwitch } from './SessionWorkspace'
import type { SessionWorkspaceState } from './types'

const doubles = vi.hoisted(() => ({ bootstrap: vi.fn(), installationSetupStatus: vi.fn().mockResolvedValue(null), startSession: vi.fn(), sessionWorkspace: vi.fn(), switchSessionChild: vi.fn(), completeAgentTurn: vi.fn(),
  props: null as null | { initial: SessionWorkspaceState; onSwitchChild: VoiceProfileSwitch } }))
vi.mock('./api', () => ({ createParentDraftRecoveryPort: () => undefined, localApi: doubles, resetRuntimeCache: vi.fn(), requestParentLocal: vi.fn(), ApiError: class extends Error {} }))
vi.mock('./LocalInputs', () => ({ HouseholdStandbyVoiceInput: () => null }))
vi.mock('./DeviceAvailabilityStrip', () => ({ DeviceAvailabilityStrip: () => null }))
vi.mock('./SessionWorkspace', () => ({ SessionWorkspace: (props: NonNullable<typeof doubles.props>) => {
  doubles.props = props
  return <div data-testid="active-profile">{props.initial.session.child_id}:{props.initial.session.session_id}</div>
} }))
import { App } from './App'
import { acknowledgeProfileSwitch, prepareProfileSwitch, readProfileSwitchHandoff, recordProfileSwitchDestination } from './profileSwitchHandoff'

const source = { session: { child_id: 'child-a', session_id: 'source-session', status: 'active' } } as SessionWorkspaceState
const target = { session: { child_id: 'child-b', session_id: 'target-session', status: 'active' } } as SessionWorkspaceState
const input = { turnId: 'profile-turn', sourceSessionId: 'source-session', sourceChildId: 'child-a', targetChildId: 'child-b', clientActionId: 'profile-turn-switch' }
const bootstrap = { children: [{ child_id: 'child-a', nickname: '甲', grade: '四年级' }, { child_id: 'child-b', nickname: '乙', grade: '初三' }],
  active_session: source.session, devices: [], model: {}, privacy_paused: false }
const completed = { agent_turn_id: input.turnId, tool_status: 'completed' }

async function beginSwitch(): Promise<void> {
  const saved = prepareProfileSwitch(input)
  await doubles.props!.onSwitchChild(input.targetChildId, async output => {
    const next = recordProfileSwitchDestination(saved, output)
    void acknowledgeProfileSwitch(next)
  }, input.clientActionId)
}

describe('App restores profile handoffs', () => {
  beforeEach(() => {
    localStorage.clear()
    doubles.props = null
    doubles.installationSetupStatus.mockResolvedValue(null)
    doubles.bootstrap.mockResolvedValue(bootstrap)
    doubles.sessionWorkspace.mockImplementation(async (id: string) => id === source.session.session_id ? source : target)
    doubles.switchSessionChild.mockResolvedValue(target.session)
    doubles.completeAgentTurn.mockResolvedValue(completed)
  })
  afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.resetAllMocks(); localStorage.clear(); delete window.desktop })

  it('activates the validated target despite ack loss and finishes the same receipt after remount', async () => {
    doubles.completeAgentTurn.mockRejectedValueOnce(new Error('lost acknowledgement')).mockResolvedValue(completed)
    const first = render(<App />)
    expect(await screen.findByTestId('active-profile')).toHaveTextContent('child-a:source-session')
    await act(beginSwitch)
    expect(screen.getByTestId('active-profile')).toHaveTextContent('child-b:target-session')
    expect(readProfileSwitchHandoff()).toMatchObject({ activated: true, receipt: { output: { target_session_id: 'target-session' } } })
    first.unmount()
    doubles.bootstrap.mockResolvedValue({ ...bootstrap, active_session: target.session })
    render(<App />)
    expect(await screen.findByTestId('active-profile')).toHaveTextContent('child-b:target-session')
    await waitFor(() => expect(readProfileSwitchHandoff()).toBeNull())
    expect(doubles.completeAgentTurn.mock.calls[0]).toEqual(doubles.completeAgentTurn.mock.calls[1])
    expect(doubles.switchSessionChild).toHaveBeenCalledTimes(1)
    expect(doubles.startSession).not.toHaveBeenCalled()
    expect(doubles.completeAgentTurn.mock.calls.every((call) => call[2] === 'completed')).toBe(true)
  })

  it('recovers a committed switch when its destination fetch failed, without another switch', async () => {
    render(<App />)
    await screen.findByTestId('active-profile')
    doubles.sessionWorkspace.mockRejectedValueOnce(new Error('target unavailable')).mockResolvedValue(target)
    await act(async () => { await expect(beginSwitch()).rejects.toThrow('target unavailable') })
    expect(screen.queryByTestId('active-profile')).not.toBeInTheDocument()
    expect(readProfileSwitchHandoff()?.receipt?.output.target_session_id).toBe('target-session')
    expect(doubles.completeAgentTurn).not.toHaveBeenCalled()
    await act(async () => { window.dispatchEvent(new Event('online')) })
    expect(await screen.findByTestId('active-profile')).toHaveTextContent('child-b:target-session')
    expect(doubles.switchSessionChild).toHaveBeenCalledTimes(1)
    expect(doubles.startSession).not.toHaveBeenCalled()
    await waitFor(() => expect(readProfileSwitchHandoff()).toBeNull())
  })

  it('does not acknowledge or activate a late switch on an unmounted app; remount recovers its destination', async () => {
    let resolveSwitch!: (session: typeof target.session) => void
    doubles.switchSessionChild.mockImplementationOnce(() => new Promise((resolve) => { resolveSwitch = resolve }))
    const first = render(<App />)
    await screen.findByTestId('active-profile')
    let switching!: Promise<void>
    act(() => { switching = beginSwitch() })
    first.unmount()
    await act(async () => { resolveSwitch(target.session); await switching })
    expect(doubles.completeAgentTurn).not.toHaveBeenCalled()
    expect(readProfileSwitchHandoff()?.activated).not.toBe(true)
    render(<App />)
    expect(await screen.findByTestId('active-profile')).toHaveTextContent('child-b:target-session')
    expect(doubles.switchSessionChild).toHaveBeenCalledTimes(1)
    expect(doubles.startSession).not.toHaveBeenCalled()
  })

  it('replays the original switch ID after a committed response was lost before reload', async () => {
    doubles.switchSessionChild.mockRejectedValueOnce(new Error('response lost')).mockResolvedValue(target.session)
    const first = render(<App />)
    await screen.findByTestId('active-profile')
    await act(async () => { await expect(beginSwitch()).rejects.toThrow('response lost') })
    expect(readProfileSwitchHandoff()?.receipt).toBeUndefined()
    first.unmount()
    render(<App />)
    expect(await screen.findByTestId('active-profile')).toHaveTextContent('child-b:target-session')
    expect(doubles.switchSessionChild.mock.calls).toEqual([
      ['source-session', 'child-b', 'profile-turn-switch'], ['source-session', 'child-b', 'profile-turn-switch'],
    ])
    expect(doubles.startSession).not.toHaveBeenCalled()
  })

  it('does not use an old receipt awaiting ack to reopen a target session that is no longer active', async () => {
    render(<App />)
    await screen.findByTestId('active-profile')
    doubles.completeAgentTurn.mockRejectedValue(new Error('offline'))
    await act(beginSwitch)
    cleanup()
    doubles.bootstrap.mockResolvedValue({ ...bootstrap, active_session: null })
    doubles.sessionWorkspace.mockClear()
    doubles.completeAgentTurn.mockResolvedValue(completed)
    render(<App />)
    await screen.findAllByText('甲')
    expect(screen.queryByTestId('active-profile')).not.toBeInTheDocument()
    expect(doubles.sessionWorkspace).not.toHaveBeenCalled()
    expect(doubles.switchSessionChild).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(readProfileSwitchHandoff()).toBeNull())
  })
})
