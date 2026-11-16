import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { App, pauseSessionBeforeHome } from './App'
import { localApi, resetRuntimeCache } from './api'

const bootstrap = {
  children: [
    { child_id: 'child-1', nickname: '小航', grade: '初三', curriculum_region: '江苏南京', status: 'active' },
    { child_id: 'child-2', nickname: '小语', grade: '四年级', curriculum_region: '江苏南京', status: 'active' }
  ],
  active_session: null,
  privacy_paused: false,
  model: { provider: 'qwen', model: 'qwen3.7-plus', voice_intent_model: 'qwen3.8-flash', configured: true },
  cloud_permissions: { cloud_model_text_allowed: true, cloud_audio_allowed: true },
  devices: [
    { device_type: 'camera', status: 'unavailable', message: '等待设备到货' },
    { device_type: 'document_camera', status: 'unavailable', message: '等待设备到货' },
    { device_type: 'microphone', status: 'ready', message: '可用' }
  ]
}

const readyDesktop = () => ({ getRuntimeConfig: vi.fn().mockResolvedValue({ localApiUrl: 'http://127.0.0.1:8765', sessionToken: 'public-ready-token', status: 'ready' }), getQwenRealtimeSettings: vi.fn().mockResolvedValue({ configured: true, enabled: true }) })
const completedInstallation = { schema: 'hsc.installation-setup.v1', complete: true, parent_pin_configured: true, smtp_configured: true, smtp_secret_check_required: false, active_child_count: 2, children_missing_email_recipient: 0, children_missing_email_test: 0, next_step: 'complete' } as const

describe('App', () => {
  beforeEach(() => {
    resetRuntimeCache()
    window.desktop = readyDesktop() as never
    vi.spyOn(localApi, 'installationSetupStatus').mockResolvedValue(completedInstallation)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => bootstrap }))
  })

  afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks() })

  it('opens only parent recovery when the service reports the limited rotation mode', async () => {
    vi.spyOn(localApi, 'bootstrap').mockRejectedValue(new Error('limited'))
    vi.spyOn(localApi, 'parentRestoreRecoveryHealth').mockResolvedValue({ status: 'rotation_auth_only', normal_business_ready: false })
    vi.spyOn(localApi, 'parentStatus').mockResolvedValue({ configured: true } as never)
    render(<App />)
    expect(await screen.findByLabelText('家长 PIN')).toBeInTheDocument()
    expect(screen.queryByText('小航')).not.toBeInTheDocument()
    expect(screen.getByText('需要家长核对密钥更新')).toBeInTheDocument()
  })

  it('keeps a visible warning when the current runtime uses the old configuration', async () => {
    window.desktop = { ...readyDesktop(), getRuntimeConfig: vi.fn().mockResolvedValue({ localApiUrl: 'http://127.0.0.1:8765', sessionToken: 'public-ready-token', status: 'ready', rotationKeyUpdatePending: true }) } as never
    render(<App />)
    expect(await screen.findByText(/新密钥尚未更新，本次正在使用原配置/)).toBeInTheDocument()
  })

  it('shows both isolated child profiles from the backend', async () => {
    render(<App />)
    expect(await screen.findByText('小航')).toBeInTheDocument()
    expect(screen.getByText('小语')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '开始学习' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '错题本' })).not.toBeInTheDocument()
    expect(screen.getByText('首次使用时，按住空格说“初三”')).toBeInTheDocument()
    expect(screen.getByText('首次使用时，按住空格说“四年级”')).toBeInTheDocument()
  })

  it('keeps child workflows behind the default household Space voice entry', async () => {
    render(<App />)
    await screen.findByText('小航')
    expect(document.querySelector('.household-standby-voice')).toHaveTextContent(/正在准备家庭唤醒监听|按住空格/)
    expect(screen.getByRole('button', { name: /家长中心/ })).toBeEnabled()
    expect(screen.getAllByRole('button')).toHaveLength(1)
  })

  it('shows a recoverable error when the local service is offline', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
    render(<App />)
    expect(await screen.findByText('暂时无法进入')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button', { name: /重启本地服务/ })).toBeEnabled())
  })

  it('keeps the installed child surface locked when mandatory setup status cannot be verified', async () => {
    vi.spyOn(localApi, 'bootstrap').mockResolvedValue(bootstrap as never)
    vi.spyOn(localApi, 'installationSetupStatus').mockRejectedValue(new Error('setup endpoint unavailable'))
    window.desktop = { ...readyDesktop(), getReportDeliverySecretStatus: vi.fn() } as never
    render(<App />)
    expect(await screen.findByText('暂时无法进入')).toBeInTheDocument()
    expect(screen.queryByText('小航')).not.toBeInTheDocument()
  })

  it('automatically restarts the local service once and refreshes its random port after a failure', async () => {
    const restartLocalService = vi.fn().mockResolvedValue({
      localApiUrl: 'http://127.0.0.1:22222', sessionToken: 'new-token', status: 'ready'
    })
    window.desktop = { ...readyDesktop(),
      getRuntimeConfig: vi.fn()
        .mockResolvedValueOnce({ localApiUrl: 'http://127.0.0.1:11111', sessionToken: 'old-token', status: 'ready' })
        .mockResolvedValueOnce({ localApiUrl: 'http://127.0.0.1:11111', sessionToken: 'old-token', status: 'error', error: 'crashed' })
        .mockResolvedValue({ localApiUrl: 'http://127.0.0.1:22222', sessionToken: 'new-token', status: 'ready' }),
      restartLocalService,
      setAutoLaunch: vi.fn(),
      getAutoLaunch: vi.fn(),
      setDeviceTestAwake: vi.fn(),
      setLearningSessionAwake: vi.fn((_sessionId: string, _leaseId: string, enabled: boolean) => ({ active: enabled, held: enabled })),
      getVideoHardwareInventory: vi.fn(),
      chooseSeykKnowledgeRoot: vi.fn(),
      chooseCurriculumStandardsRoot: vi.fn(),
      openLocalPath: vi.fn(),
      getModelSettings: vi.fn(),
      saveModelSettings: vi.fn(),
      clearModelSettings: vi.fn(),
      testModelSettings: vi.fn()
    }
    vi.stubGlobal('fetch', vi.fn()
      .mockRejectedValueOnce(new Error('service crashed'))
      .mockResolvedValue({ ok: true, json: async () => bootstrap }))

    render(<App />)

    expect(await screen.findByText('小航')).toBeInTheDocument()
    expect(restartLocalService).toHaveBeenCalledTimes(1)
    expect(fetch).toHaveBeenCalledWith(
      'http://127.0.0.1:22222/v1/bootstrap',
      expect.objectContaining({ headers: expect.objectContaining({ 'X-HSC-Session': 'new-token' }) })
    )
  })

  it('returns to voice standby automatically when the main process finishes its own recovery', async () => {
    vi.useFakeTimers()
    const restartLocalService = vi.fn()
    window.desktop = { ...readyDesktop(),
      getRuntimeConfig: vi.fn()
        .mockResolvedValueOnce({ localApiUrl: 'http://127.0.0.1:11111', sessionToken: 'old-token', status: 'ready' })
        .mockResolvedValueOnce({ localApiUrl: 'http://127.0.0.1:11111', sessionToken: 'old-token', status: 'starting', recoveryAttempt: 1, nextRetryAt: new Date().toISOString() })
        .mockResolvedValue({ localApiUrl: 'http://127.0.0.1:22222', sessionToken: 'new-token', status: 'ready' }),
      restartLocalService,
      setAutoLaunch: vi.fn(), getAutoLaunch: vi.fn(), setDeviceTestAwake: vi.fn(), getVideoHardwareInventory: vi.fn(),
      setLearningSessionAwake: vi.fn((_sessionId: string, _leaseId: string, enabled: boolean) => ({ active: enabled, held: enabled })),
      chooseSeykKnowledgeRoot: vi.fn(), chooseCurriculumStandardsRoot: vi.fn(), openLocalPath: vi.fn(),
      getModelSettings: vi.fn(), saveModelSettings: vi.fn(), clearModelSettings: vi.fn(), testModelSettings: vi.fn(),
    }
    vi.stubGlobal('fetch', vi.fn()
      .mockRejectedValueOnce(new Error('service crashed'))
      .mockResolvedValue({ ok: true, json: async () => bootstrap }))

    render(<App />)
    await vi.waitFor(() => expect(screen.getByText('暂时无法进入')).toBeInTheDocument())
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
    await vi.waitFor(() => expect(screen.getByText('小航')).toBeInTheDocument())
    expect(restartLocalService).not.toHaveBeenCalled()
  })

  it('reloads the home state after an atomic credential clear restarts the local service', async () => {
    const load = vi.spyOn(localApi, 'bootstrap').mockResolvedValue(bootstrap as never)
    render(<App />)
    expect(await screen.findByText('小航')).toBeInTheDocument()
    expect(load).toHaveBeenCalledTimes(1)

    window.dispatchEvent(new Event('hsc-runtime-restarted'))

    await waitFor(() => expect(load).toHaveBeenCalledTimes(2))
    expect(screen.getByText('小语')).toBeInTheDocument()
  })

  it('shows a recoverable app-level error when credential clearing cannot restart the service', async () => {
    vi.spyOn(localApi, 'bootstrap').mockResolvedValue(bootstrap as never)
    render(<App />)
    expect((await screen.findAllByText('小航')).length).toBeGreaterThan(0)

    window.desktop!.getRuntimeConfig = vi.fn().mockResolvedValue({ localApiUrl: 'http://127.0.0.1:8765', sessionToken: 'public-ready-token', status: 'error', error: 'public restart failure' })
    window.dispatchEvent(new CustomEvent('hsc-runtime-restarted', { detail: { error: '凭据和通道配置已清，但本地服务重启失败。' } }))

    expect(await screen.findByText('凭据和通道配置已清，但本地服务重启失败。')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /重启本地服务/ })).toBeEnabled()
  })

  it('pauses and releases the session before refreshing the home device state', async () => {
    const order: string[] = []
    const pause = vi.spyOn(localApi, 'pauseObservation').mockImplementation(async () => {
      order.push('backend_pause')
      return { session_id: 'session-1', child_id: 'child-2', status: 'active', state: 'check_in', started_at: '', last_checkpoint_at: '', observation_revision: 0, observation_generation: 'a'.repeat(64), observation_status: 'paused', snooze_until: null, recall_declared_empty: 0, homework_declared_empty: 0 }
    })
    vi.spyOn(localApi, 'bootstrap').mockImplementation(async () => {
      order.push('bootstrap')
      return { ...bootstrap, privacy_paused: true } as never
    })
    window.addEventListener('hsc-privacy-pause', () => order.push('local_pause'), { once: true })

    const result = await pauseSessionBeforeHome('session-1')

    expect(order).toEqual(['local_pause', 'backend_pause', 'bootstrap'])
    expect(pause).toHaveBeenCalledWith('session-1')
    expect(result.privacy_paused).toBe(true)
  })

  it('does not rewrite an already completed session when returning home', async () => {
    const order: string[] = []
    const pause = vi.spyOn(localApi, 'pauseObservation')
    vi.spyOn(localApi, 'bootstrap').mockImplementation(async () => {
      order.push('bootstrap')
      return bootstrap as never
    })
    window.addEventListener('hsc-privacy-pause', () => order.push('local_release'), { once: true })

    await pauseSessionBeforeHome('session-completed', false)

    expect(order).toEqual(['local_release', 'bootstrap'])
    expect(pause).not.toHaveBeenCalled()
  })
})
