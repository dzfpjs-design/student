import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
const h = vi.hoisted(() => ({ bootstrap: vi.fn(), installationSetupStatus: vi.fn(), startSession: vi.fn(), sessionWorkspace: vi.fn(),
  realtime: vi.fn(), onWake: null as any, openParent: null as any, parentSection: undefined as any,
  pending: null as any, resolveHandoff: vi.fn(), markHandoff: vi.fn(), acknowledge: vi.fn(), household: vi.fn() }))
vi.mock('./api', () => ({ createParentDraftRecoveryPort: () => undefined, localApi: h, resetRuntimeCache: vi.fn(), requestParentLocal: vi.fn(), ApiError: class extends Error {} }))
vi.mock('./LocalInputs', () => ({ HouseholdStandbyVoiceInput: (props: any) => { h.onWake = props.onWake; h.household(props.enabled); return <div data-testid="household">本机配置已读取，连接需实际验证</div> } }))
vi.mock('./SessionWorkspace', () => ({ SessionWorkspace: ({ initial }: any) => <div data-testid="workspace">{initial.session.child_id}:{initial.session.session_id}</div> }))
vi.mock('./DeviceAvailabilityStrip', () => ({ DeviceAvailabilityStrip: () => null }))
vi.mock('./InstallationSetupWizard', () => ({ InstallationSetupWizard: () => <div>原PIN和SMTP实发验证流程</div> }))
vi.mock('./ParentCenter', () => ({ ParentCenter: ({ onClose, initialSection }: any) => { h.parentSection = initialSection; return <div>原家长PIN保护的配置中心<button onClick={onClose}>返回继续</button></div> } }))
vi.mock('./profileSwitchHandoff', () => ({ readProfileSwitchHandoff: () => h.pending, resolveProfileSwitchDestination: h.resolveHandoff, markProfileSwitchActivated: h.markHandoff, acknowledgeProfileSwitch: h.acknowledge }))
import { App } from './App'

const child = { child_id: 'public-child', nickname: '公开同学', grade: '初三', curriculum_region: '公开区域' }
const active = { session_id: 'public-original', child_id: child.child_id, status: 'active' }
const bootstrap = () => ({ children: [child], active_session: null as any, devices: [], privacy_paused: false,
  model: { configured: true }, cloud_permissions: { cloud_model_text_allowed: true, cloud_audio_allowed: true } })
let data = bootstrap()
beforeEach(() => {
  data = bootstrap(); h.pending = null; h.onWake = null; h.openParent = null; h.parentSection = undefined
  h.bootstrap.mockImplementation(async () => data); h.installationSetupStatus.mockResolvedValue({ schema: 'hsc.installation-setup.v1', complete: true, smtp_secret_check_required: true })
  h.realtime.mockResolvedValue({ configured: true, enabled: true }); h.startSession.mockResolvedValue(active)
  h.sessionWorkspace.mockImplementation(async (id: string) => ({ session: { ...active, session_id: id } }))
  window.desktop = { getReportDeliverySecretStatus: vi.fn(async () => ({ smtpPasswordConfigured: true })), getQwenRealtimeSettings: h.realtime,
    getRuntimeConfig: vi.fn(async () => ({ status: 'ready' })),
    getWakeWordSettings: vi.fn(async () => ({ enabled: true, wakeWord: '小知' })), onOpenParentCenter: (callback: any) => { h.openParent = callback; return () => {} } } as never
})
afterEach(() => { cleanup(); delete window.desktop; localStorage.clear(); vi.clearAllMocks() })

it.each([
  ['model absent', () => { data.model.configured = false }],
  ['voice absent', () => { h.realtime.mockResolvedValue({ configured: false, enabled: false }) }],
  ['voice disabled', () => { h.realtime.mockResolvedValue({ configured: true, enabled: false }) }],
  ['audio not consented', () => { data.cloud_permissions.cloud_audio_allowed = false }],
  ['text not consented', () => { data.cloud_permissions.cloud_model_text_allowed = false }],
  ['permissions unconfirmed', () => { delete (data as any).cloud_permissions }],
  ['local settings unreadable', () => { h.realtime.mockRejectedValue(Error('PUBLIC_LOCAL_READ_FAILURE')) }],
] as const)('blocks Home microphone and automatic old-session resume when %s', async (_name, configure) => {
  configure(); data.active_session = active
  render(<App />)
  expect(await screen.findByText('请家长完成本机学习设置')).toBeInTheDocument()
  expect(screen.queryByTestId('household')).not.toBeInTheDocument(); expect(screen.queryByTestId('workspace')).not.toBeInTheDocument()
  expect(h.household).not.toHaveBeenCalled(); expect(h.startSession).not.toHaveBeenCalled(); expect(h.sessionWorkspace).not.toHaveBeenCalled()
  expect(h.resolveHandoff).not.toHaveBeenCalled(); expect(h.acknowledge).not.toHaveBeenCalled()
})

it('preserves a configured existing profile exact old-session resume without creating another session', async () => {
  data.active_session = active; render(<App />)
  expect(await screen.findByTestId('workspace')).toHaveTextContent('public-child:public-original')
  expect(h.sessionWorkspace).toHaveBeenCalledExactlyOnceWith('public-original'); expect(h.startSession).not.toHaveBeenCalled()
})

it.each(['paused', 'recovery'])('keeps a fresh Home microphone closed during %s and permits explicit profile selection', async (mode) => {
  if (mode === 'paused') data.privacy_paused = true
  else Object.assign(data, { recovery: { sensors_forced_paused: true } })
  render(<App />)
  expect(await screen.findByTestId('household')).toBeInTheDocument()
  expect(h.household).toHaveBeenLastCalledWith(false)
  await act(async () => { await h.onWake({ childId: child.child_id, wakeWord: '小知', configured: true }, '') })
  expect(h.startSession).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '进入学习' }))
  expect(await screen.findByTestId('workspace')).toHaveTextContent('public-child:public-original')
  expect(h.startSession).toHaveBeenCalledExactlyOnceWith(child.child_id)
})

it('keeps the original PIN/SMTP gate ahead of child setup and does not mount the microphone', async () => {
  h.installationSetupStatus.mockResolvedValue({ schema: 'hsc.installation-setup.v1', complete: false, smtp_secret_check_required: false })
  data.model.configured = false; render(<App />)
  expect(await screen.findByText('原PIN和SMTP实发验证流程')).toBeInTheDocument()
  expect(h.household).not.toHaveBeenCalled(); expect(screen.queryByText('打开家长配置')).not.toBeInTheDocument()
})

it('allows a previously used family to resume while making unverified mail visible to the parent', async () => {
  h.installationSetupStatus.mockResolvedValue({schema:'hsc.installation-setup.v1',complete:false,parent_pin_configured:true,existing_family:true,smtp_secret_check_required:true})
  render(<App />)
  expect(await screen.findByTestId('household')).toBeInTheDocument()
  expect(screen.queryByText('原PIN和SMTP实发验证流程')).not.toBeInTheDocument()
  await act(async () => { h.openParent() })
  expect(await screen.findByText('原家长PIN保护的配置中心')).toBeInTheDocument()
  expect(screen.getByText(/日报邮件尚未完成验证/)).toBeInTheDocument()
})

it('does not bypass first setup without a configured parent PIN even if history is reported', async () => {
  h.installationSetupStatus.mockResolvedValue({schema:'hsc.installation-setup.v1',complete:false,parent_pin_configured:false,existing_family:true,smtp_secret_check_required:false})
  render(<App />)
  expect(await screen.findByText('原PIN和SMTP实发验证流程')).toBeInTheDocument()
  expect(screen.queryByTestId('household')).not.toBeInTheDocument()
})

it('keeps first setup blocking for a new family even when the parent has set a PIN', async () => {
  h.installationSetupStatus.mockResolvedValue({schema:'hsc.installation-setup.v1',complete:false,parent_pin_configured:true,existing_family:false,smtp_secret_check_required:false})
  render(<App />)
  expect(await screen.findByText('原PIN和SMTP实发验证流程')).toBeInTheDocument()
  expect(screen.queryByTestId('household')).not.toBeInTheDocument()
})

it('keeps a used family available when the saved SMTP secret cannot be read', async () => {
  h.installationSetupStatus.mockResolvedValue({schema:'hsc.installation-setup.v1',complete:true,parent_pin_configured:true,existing_family:true,smtp_secret_check_required:true})
  vi.mocked(window.desktop!.getReportDeliverySecretStatus!).mockRejectedValueOnce(Error('unavailable'))
  render(<App />)
  expect(await screen.findByTestId('household')).toBeInTheDocument()
  await act(async () => { h.openParent() })
  expect(await screen.findByText(/日报邮件尚未完成验证/)).toBeInTheDocument()
})

it('opens the original protected settings section and returns through fresh stores; remount does not rely on a completed flag', async () => {
  data.cloud_permissions.cloud_audio_allowed = false
  const first = render(<App />); fireEvent.click(await screen.findByText('打开家长配置'))
  expect(await screen.findByText('原家长PIN保护的配置中心')).toBeInTheDocument(); expect(h.parentSection).toBe('voice_setup')
  data = bootstrap(); fireEvent.click(screen.getByText('返回继续'))
  expect(await screen.findByTestId('household')).toBeInTheDocument()
  first.unmount(); data.model.configured = false; render(<App />)
  expect(await screen.findByText('请家长完成本机学习设置')).toBeInTheDocument()
  expect(screen.queryByTestId('household')).not.toBeInTheDocument()
})

it('does not consume or activate a stored profile handoff until configuration is rechecked', async () => {
  data.model.configured = false; h.pending = { activated: false, turnId: 'public-handoff' }
  h.resolveHandoff.mockResolvedValue({ handoff: h.pending, workspace: { session: active } })
  render(<App />); await screen.findByText('请家长完成本机学习设置')
  expect(h.resolveHandoff).not.toHaveBeenCalled(); expect(h.markHandoff).not.toHaveBeenCalled()
  data = bootstrap(); fireEvent.click(screen.getByText('重新核对配置'))
  expect(await screen.findByTestId('workspace')).toHaveTextContent('public-original')
  expect(h.resolveHandoff).toHaveBeenCalledOnce(); expect(h.markHandoff).toHaveBeenCalledOnce(); expect(h.startSession).not.toHaveBeenCalled()
})

it('rejects a previous Home wake callback while a fresh configuration read is pending', async () => {
  render(<App />); await screen.findByTestId('household'); const oldWake = h.onWake
  let release!: (value: any) => void
  h.realtime.mockImplementationOnce(() => new Promise(resolve => { release = resolve }))
  await act(async () => { window.dispatchEvent(new CustomEvent('hsc-runtime-restarted')) })
  await act(async () => { await oldWake({ childId: child.child_id, wakeWord: '小知', configured: true }, '公开请求') })
  expect(h.startSession).not.toHaveBeenCalled()
  await act(async () => { release({ configured: false, enabled: false }) })
  expect(await screen.findByText('请家长完成本机学习设置')).toBeInTheDocument()
})

it('returns from an old workspace through current settings and keeps a now-unconfigured session unopened', async () => {
  data.active_session = active; render(<App />); await screen.findByTestId('workspace')
  await act(async () => { h.openParent() }); await screen.findByText('原家长PIN保护的配置中心')
  data.model.configured = false; const previousReads = h.sessionWorkspace.mock.calls.length
  fireEvent.click(screen.getByText('返回继续')); await screen.findByText('请家长完成本机学习设置')
  expect(h.sessionWorkspace.mock.calls.length).toBe(previousReads); expect(h.startSession).not.toHaveBeenCalled()
})

it('a parent request during a pending configuration read cannot briefly mount Home or automatically resume the old child', async () => {
  let release!: (value: any) => void
  h.realtime.mockImplementationOnce(() => new Promise(resolve => { release = resolve }))
  data.active_session = active; render(<App />)
  await waitFor(() => expect(h.realtime).toHaveBeenCalledOnce())
  await act(async () => { h.openParent() })
  await act(async () => { release({ configured: true, enabled: true }) })
  expect(await screen.findByText('原家长PIN保护的配置中心')).toBeInTheDocument()
  expect(h.household).not.toHaveBeenCalled(); expect(h.sessionWorkspace).not.toHaveBeenCalled(); expect(h.startSession).not.toHaveBeenCalled()
})
