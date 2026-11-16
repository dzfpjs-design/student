import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
const leaf = vi.hoisted(() => ({ props: null as null | { initial: { session: { session_id: string; child_id: string } }; onSwitchChild: (childId: string, beforeActivate: (output: any) => Promise<void>, actionId: string) => Promise<void> } }))
vi.mock('./LocalInputs', () => ({ HouseholdStandbyVoiceInput: () => null }))
vi.mock('./SessionWorkspace', () => ({ SessionWorkspace: (props: NonNullable<typeof leaf.props>) => { leaf.props = props; return <div data-testid="active-profile">{props.initial.session.child_id}:{props.initial.session.session_id}</div> } }))
vi.mock('./ParentCenter', () => ({ ParentCenter: () => <div>家长操作区</div> }))
vi.mock('./DeviceAvailabilityStrip', () => ({ DeviceAvailabilityStrip: () => null }))
import { App } from './App'
import { resetRuntimeCache } from './api'
import { acknowledgeProfileSwitch, prepareProfileSwitch, readProfileSwitchHandoff, recordProfileSwitchDestination } from './profileSwitchHandoff'
const old = { status: 'ready' as const, localApiUrl: 'http://127.0.0.1:19121', sessionToken: 'PUBLIC-A' }
const fresh = { status: 'ready' as const, localApiUrl: 'http://127.0.0.1:19122', sessionToken: 'PUBLIC-B' }
const children = ['a', 'b'].map(id => ({ child_id: 'synthetic-' + id, nickname: id, grade: '四年级', curriculum_region: '江苏南京', status: 'active' }))
const source = { session_id: 'synthetic-source', child_id: children[0].child_id, status: 'active', observation_status: 'paused', observation_revision: 0, observation_generation: 'a'.repeat(64) }
const target = { ...source, session_id: 'synthetic-target', child_id: children[1].child_id, observation_generation: 'b'.repeat(64) }
const input = { turnId: 'synthetic-profile-turn', sourceSessionId: source.session_id, sourceChildId: source.child_id, targetChildId: target.child_id, clientActionId: 'synthetic-profile-action' }
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
beforeEach(() => { localStorage.clear(); resetRuntimeCache(); leaf.props = null })
afterEach(() => { cleanup(); delete window.desktop; resetRuntimeCache(); vi.unstubAllGlobals(); vi.restoreAllMocks() })
async function mount(mode: 'lost-ack' | 'identity-during-target-read') {
  const calls: { path: string; port: string; body?: unknown }[] = []
  let outcomeCalls = 0, identityChanged = false, switchCalls = 0
  window.desktop = { getRuntimeConfig: vi.fn(async () => identityChanged ? fresh : old) } as unknown as Window['desktop']
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const { pathname: path, port } = new URL(url)
    calls.push({ path, port, body: init?.body ? JSON.parse(String(init.body)) : undefined })
    if (path === '/v1/bootstrap') return response({ children, active_session: identityChanged ? target : source, devices: [], model: {}, privacy_paused: false })
    if (path === '/v1/install-setup/status') return response({})
    if (path === '/v1/sessions/' + source.session_id) return response({ session: source })
    if (path === '/v1/sessions/' + source.session_id + '/switch-child') { switchCalls++; return response(target) }
    if (path === '/v1/sessions/' + target.session_id) {
      if (mode === 'identity-during-target-read' && port === '19121') { identityChanged = true; throw new TypeError('PUBLIC_SYNTHETIC_TARGET_OFFLINE') }
      return response({ session: target })
    }
    if (path === '/v1/agent-turns/' + input.turnId + '/outcome') {
      outcomeCalls++
      if (mode === 'lost-ack' && outcomeCalls === 1) return response({ code: 'PUBLIC_BUSINESS_TEMPORARY', message: 'public synthetic response loss' }, 503)
      return response({ agent_turn_id: input.turnId, tool_status: 'completed' })
    }
    throw new Error('Unexpected synthetic endpoint: ' + path)
  }))
  render(<App />)
  await screen.findByTestId('active-profile')
  return { calls, counts: () => ({ outcomeCalls, switchCalls }) }
}
it('retries an already activated exact receipt on online without changing the selected workspace', async () => {
  const f = await mount('lost-ack')
  const pending = prepareProfileSwitch(input)
  let firstAck!: Promise<boolean>
  await act(async () => {
    await leaf.props!.onSwitchChild(target.child_id, async output => {
      const saved = recordProfileSwitchDestination(pending, output)
      firstAck = acknowledgeProfileSwitch(saved)
    }, pending.clientActionId)
    expect(await firstAck).toBe(false)
  })
  expect(screen.getByTestId('active-profile')).toHaveTextContent(target.child_id + ':' + target.session_id)
  expect(readProfileSwitchHandoff()?.activated).toBe(true)
  await act(async () => { window.dispatchEvent(new Event('online')); await new Promise(resolve => setTimeout(resolve, 0)) })
  const saved = readProfileSwitchHandoff()
  let nextSwitchBlocked = false
  try { prepareProfileSwitch({ ...input, turnId: 'synthetic-next-turn', sourceSessionId: target.session_id, sourceChildId: target.child_id, targetChildId: source.child_id, clientActionId: 'synthetic-next-action' }) }
  catch { nextSwitchBlocked = true }
  expect(f.counts().outcomeCalls, 'the original bounded retry must confirm the same persisted receipt').toBe(2)
  expect(saved).toBeNull()
  expect(nextSwitchBlocked).toBe(false)
  expect(f.counts().switchCalls).toBe(1)
  expect(screen.getByTestId('active-profile')).toHaveTextContent(target.child_id + ':' + target.session_id)
})
it('retains a committed pending handoff when identity changes during the exact target workspace read', async () => {
  const f = await mount('identity-during-target-read')
  const pending = prepareProfileSwitch(input)
  await act(async () => {
    await leaf.props!.onSwitchChild(target.child_id, async output => {
      void acknowledgeProfileSwitch(recordProfileSwitchDestination(pending, output))
    }, pending.clientActionId).catch(() => {})
    await new Promise(resolve => setTimeout(resolve, 0))
    window.dispatchEvent(new Event('online'))
    await new Promise(resolve => setTimeout(resolve, 0))
  })
  expect(screen.queryByTestId('active-profile'), 'the known committed target must remain recoverable without a second switch POST').toHaveTextContent(target.child_id + ':' + target.session_id)
  expect(f.counts().switchCalls).toBe(1)
})
