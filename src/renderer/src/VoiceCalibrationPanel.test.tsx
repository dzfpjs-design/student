import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { VoiceCalibrationPanel } from './VoiceCalibrationPanel'
import { mediaDeviceInfoHash } from './mediaDevices'
import { loadPendingVoiceCalibrationDelete, loadPendingVoiceCalibrationSave, preparePendingVoiceCalibrationDelete, preparePendingVoiceCalibrationSave } from './voiceCalibrationPending'
import type { VoiceProfile } from './voiceProfile'

const originalMediaDevices = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices')
const profile: VoiceProfile = {
  version: 2, childId: 'child-primary-4', consentedAt: '2026-08-28T10:00:00.000Z', updatedAt: '2026-08-28T10:00:00.000Z',
  sampleCount: 96_000, calibrationCount: 3, energy: .12, zeroCrossingRate: .08, spectralBands: [.2, .3, .3, .2], environmentSpread: .1,
}
const serviceProfile = { ...profile, version: 3 as const }
const microphone = { system_id_hash: 'a'.repeat(64), label: 'USB Microphone', capabilities: { transport: 'browser-media-device' as const, echo_cancellation_requested: true, noise_suppression_requested: true, auto_gain_control_requested: true } }

beforeEach(() => {
  localStorage.clear()
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { enumerateDevices: vi.fn().mockResolvedValue([{ kind: 'audioinput', deviceId: 'parent-mic', groupId: '', label: 'USB Microphone' }]) } })
})
afterEach(() => {
  cleanup(); vi.restoreAllMocks()
  if (originalMediaDevices) Object.defineProperty(navigator, 'mediaDevices', originalMediaDevices)
  else Reflect.deleteProperty(navigator, 'mediaDevices')
})

describe('VoiceCalibrationPanel parent-managed persistence', () => {
  it('does not let a late persisted binding replace a parent microphone selection', async () => {
    const summaryValue = {
      child_id: 'child-primary-4', status: 'not_calibrated' as const,
      raw_audio_stored: false as const, profile: null,
      microphone: { ...microphone, label: 'Old Microphone' },
    }
    let resolveSummary: ((value: typeof summaryValue) => void) | undefined
    const delayedSummary = new Promise<typeof summaryValue>((resolve) => { resolveSummary = resolve })
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { enumerateDevices: vi.fn().mockResolvedValue([
      { kind: 'audioinput', deviceId: 'old-mic', groupId: 'old', label: 'Old Microphone' },
      { kind: 'audioinput', deviceId: 'chosen-mic', groupId: 'chosen', label: 'Chosen Microphone' },
    ]) } })
    vi.spyOn(localApi, 'voiceCalibrationSummary').mockReturnValue(delayedSummary)

    render(<VoiceCalibrationPanel childId="child-primary-4" parentManaged />)
    fireEvent.click(screen.getByRole('checkbox'))
    const selector = await screen.findByRole('combobox', { name: '选择校准麦克风' })
    await waitFor(() => expect(screen.getByRole('option', { name: 'Chosen Microphone' })).toBeInTheDocument())
    fireEvent.change(selector, { target: { value: 'chosen-mic' } })
    expect(selector).toHaveValue('chosen-mic')

    resolveSummary?.(summaryValue)
    await waitFor(() => expect(localApi.voiceCalibrationSummary).toHaveBeenCalled())
    expect(selector).toHaveValue('chosen-mic')
  })

  it('keeps the child session on the Windows default when a document camera is bound', async () => {
    const camera = { kind: 'videoinput' as const, deviceId: 'desk-camera', groupId: 'desk-video', label: 'Usb HDCamera (eba4:1303)' }
    const highCameraMic = { kind: 'audioinput' as const, deviceId: 'desk-mic', groupId: 'desk-audio', label: '麦克风 (HDAudio) (eba4:1303)' }
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { enumerateDevices: vi.fn().mockResolvedValue([
      { kind: 'audioinput', deviceId: 'default', groupId: '', label: 'Default - L523 (1d6b:0102)' },
      { kind: 'audioinput', deviceId: 'laptop-mic', groupId: 'laptop', label: '麦克风阵列 (8086:51ca)' },
      highCameraMic,
      camera,
    ]) } })
    vi.spyOn(localApi, 'sessionVoiceCalibrationSummary').mockResolvedValue({
      child_id: 'child-primary-4', status: 'not_calibrated', raw_audio_stored: false, profile: null,
    })
    vi.spyOn(localApi, 'deviceBindings').mockResolvedValue([{
      device_type: 'document_camera', system_id_hash: await mediaDeviceInfoHash(camera), label: camera.label,
      capabilities: {}, verified_at: '', updated_at: '',
    }])
    const bound = vi.fn()
    const bindingReady = vi.fn()

    render(<VoiceCalibrationPanel childId="child-primary-4" sessionId="session-primary" sessionScoped onBoundAudioDeviceIdChange={bound} onMicrophoneBindingReadyChange={bindingReady} />)

    await waitFor(() => expect(bound).toHaveBeenCalledWith(undefined))
    expect(bindingReady).toHaveBeenLastCalledWith(true)
    expect(await screen.findByText('正在使用Windows默认麦克风。')).toBeInTheDocument()
  })

  it('hydrates only the current session child from a persisted service profile after renderer storage is cleared', async () => {
    const summary = vi.spyOn(localApi, 'sessionVoiceCalibrationSummary').mockResolvedValue({
      child_id: 'child-primary-4', status: 'ready', raw_audio_stored: false,
      profile: serviceProfile, vocabulary: ['奥数'], microphone,
    })
    const bound = vi.fn()
    const bindingReady = vi.fn()
    render(<VoiceCalibrationPanel childId="child-primary-4" sessionId="session-primary" sessionScoped onBoundAudioDeviceIdChange={bound} onMicrophoneBindingReadyChange={bindingReady} />)

    expect(await screen.findByText(/已恢复家长保存/)).toBeInTheDocument()
    expect(summary).toHaveBeenCalledWith('session-primary')
    await waitFor(() => expect(bound).toHaveBeenCalledWith('parent-mic'))
    expect(bindingReady).toHaveBeenCalledWith(false)
    expect(bindingReady).toHaveBeenLastCalledWith(true)
    expect(JSON.parse(localStorage.getItem('hsc.voice-profile.v2.child-primary-4') ?? '{}')).toMatchObject({ version: 2, childId: 'child-primary-4', calibrationCount: 3 })
    expect(screen.queryByRole('button', { name: '删除声音特征' })).toBeNull()
    expect(screen.getByText(/校准与麦克风绑定由家长中心管理/)).toBeInTheDocument()
  })

  it('drops a late session summary after the learning surface switches children', async () => {
    let resolveFirst: ((value: { child_id: string; status: string; raw_audio_stored: false; profile: typeof serviceProfile; vocabulary: string[]; microphone: typeof microphone }) => void) | undefined
    const first = new Promise<{ child_id: string; status: string; raw_audio_stored: false; profile: typeof serviceProfile; vocabulary: string[]; microphone: typeof microphone }>((resolve) => { resolveFirst = resolve })
    vi.spyOn(localApi, 'sessionVoiceCalibrationSummary').mockImplementation((sessionId) => sessionId === 'session-a'
      ? first
      : Promise.resolve({ child_id: 'child-b', status: 'ready', raw_audio_stored: false, profile: { ...serviceProfile, childId: 'child-b' }, vocabulary: ['B词'], microphone }))
    const view = render(<VoiceCalibrationPanel childId="child-a" sessionId="session-a" sessionScoped />)
    view.rerender(<VoiceCalibrationPanel childId="child-b" sessionId="session-b" sessionScoped />)
    expect(await screen.findByText(/B词/)).toBeInTheDocument()
    resolveFirst?.({ child_id: 'child-a', status: 'ready', raw_audio_stored: false, profile: { ...serviceProfile, childId: 'child-a' }, vocabulary: ['A词'], microphone })
    await Promise.resolve()
    expect(screen.getByText(/B词/)).toBeInTheDocument()
    expect(screen.queryByText(/A词/)).toBeNull()
    expect(localStorage.getItem('hsc.voice-profile.v2.child-a')).toBeNull()
  })

  it('restores a pending save when the service still has no calibration record', async () => {
    await preparePendingVoiceCalibrationSave('child-primary-4', profile, ['奥数'], microphone)
    vi.spyOn(localApi, 'voiceCalibrationSummary').mockResolvedValue({ child_id: 'child-primary-4', status: 'not_calibrated', raw_audio_stored: false, profile: null })
    render(<VoiceCalibrationPanel childId="child-primary-4" parentManaged />)
    expect(await screen.findByText(/待保存到本机服务/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重试保存' })).toBeInTheDocument()
  })

  it('clears an unacknowledged pending operation after a response-lost save is found on the service', async () => {
    await preparePendingVoiceCalibrationSave('child-primary-4', profile, ['奥数'], microphone)
    vi.spyOn(localApi, 'voiceCalibrationSummary').mockResolvedValue({ child_id: 'child-primary-4', status: 'ready', raw_audio_stored: false, profile: serviceProfile, vocabulary: ['奥数'], microphone })
    render(<VoiceCalibrationPanel childId="child-primary-4" parentManaged />)
    await waitFor(() => expect(loadPendingVoiceCalibrationSave('child-primary-4')).toBeNull())
    expect(await screen.findByText(/已保存三段本机环境特征|校准完成/)).toBeInTheDocument()
  })

  it('clears a stale browser-ready profile only when there is neither a service record nor a pending save', async () => {
    localStorage.setItem('hsc.voice-profile.v2.child-primary-4', JSON.stringify(profile))
    vi.spyOn(localApi, 'voiceCalibrationSummary').mockResolvedValue({ child_id: 'child-primary-4', status: 'not_calibrated', raw_audio_stored: false, profile: null })
    render(<VoiceCalibrationPanel childId="child-primary-4" parentManaged />)
    await waitFor(() => expect(localStorage.getItem('hsc.voice-profile.v2.child-primary-4')).toBeNull())
    expect(screen.queryByRole('button', { name: '重试保存' })).toBeNull()
  })

  it('retries a completed pending save with its stored microphone even when that microphone is now offline', async () => {
    const pending = await preparePendingVoiceCalibrationSave('child-primary-4', profile, ['奥数'], microphone)
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { enumerateDevices: vi.fn().mockResolvedValue([]) } })
    vi.spyOn(localApi, 'voiceCalibrationSummary').mockResolvedValue({ child_id: 'child-primary-4', status: 'not_calibrated', raw_audio_stored: false, profile: null })
    const save = vi.spyOn(localApi, 'saveVoiceCalibration').mockResolvedValue({ child_id: 'child-primary-4', status: 'ready', raw_audio_stored: false, profile: serviceProfile, vocabulary: ['奥数'], microphone })
    render(<VoiceCalibrationPanel childId="child-primary-4" parentManaged />)
    fireEvent.click(await screen.findByRole('button', { name: '重试保存' }))
    await waitFor(() => expect(save).toHaveBeenCalledWith('child-primary-4', pending.profile, pending.vocabulary, pending.microphone, pending.clientActionId))
  })

  it('does not clear local feature summaries when the service delete fails, and exposes a durable retry', async () => {
    localStorage.setItem('hsc.voice-profile.v2.child-primary-4', JSON.stringify(profile))
    vi.spyOn(localApi, 'voiceCalibrationSummary').mockResolvedValue({ child_id: 'child-primary-4', status: 'ready', raw_audio_stored: false, profile: serviceProfile, vocabulary: ['奥数'], microphone })
    vi.spyOn(localApi, 'deleteVoiceCalibration').mockRejectedValue(new Error('offline'))
    render(<VoiceCalibrationPanel childId="child-primary-4" parentManaged />)
    fireEvent.click(await screen.findByRole('button', { name: '删除声音特征' }))
    expect(await screen.findByText(/删除尚未得到本机服务确认/)).toBeInTheDocument()
    expect(localStorage.getItem('hsc.voice-profile.v2.child-primary-4')).not.toBeNull()
    expect(loadPendingVoiceCalibrationDelete('child-primary-4')).not.toBeNull()
    expect(screen.getByRole('button', { name: '重试删除' })).toBeInTheDocument()
  })

  it('keeps a delete unknown and unusable when the service cannot answer the cancel check', async () => {
    localStorage.setItem('hsc.voice-profile.v2.child-primary-4', JSON.stringify(profile))
    preparePendingVoiceCalibrationDelete('child-primary-4')
    const summary = vi.spyOn(localApi, 'voiceCalibrationSummary').mockResolvedValue({ child_id: 'child-primary-4', status: 'ready', raw_audio_stored: false, profile: serviceProfile, vocabulary: ['奥数'], microphone })
    render(<VoiceCalibrationPanel childId="child-primary-4" parentManaged />)
    await screen.findByRole('button', { name: '取消删除等待' })
    summary.mockRejectedValueOnce(new Error('offline'))
    fireEvent.click(await screen.findByRole('button', { name: '取消删除等待' }))
    expect(await screen.findByText(/无法确认本机服务当前状态/)).toBeInTheDocument()
    expect(loadPendingVoiceCalibrationDelete('child-primary-4')).not.toBeNull()
    expect(localStorage.getItem('hsc.voice-profile.v2.child-primary-4')).not.toBeNull()
    expect(screen.queryByRole('button', { name: '录第一段校准' })).toBeNull()
  })

  it('uses the service record to recover or remove local state when cancelling an unknown delete', async () => {
    localStorage.setItem('hsc.voice-profile.v2.child-primary-4', JSON.stringify(profile))
    preparePendingVoiceCalibrationDelete('child-primary-4')
    const summary = vi.spyOn(localApi, 'voiceCalibrationSummary').mockResolvedValue({ child_id: 'child-primary-4', status: 'ready', raw_audio_stored: false, profile: serviceProfile, vocabulary: ['奥数'], microphone })
    render(<VoiceCalibrationPanel childId="child-primary-4" parentManaged />)
    await screen.findByRole('button', { name: '取消删除等待' })
    summary.mockResolvedValueOnce({ child_id: 'child-primary-4', status: 'not_calibrated', raw_audio_stored: false, profile: null })
    fireEvent.click(await screen.findByRole('button', { name: '取消删除等待' }))
    await waitFor(() => expect(loadPendingVoiceCalibrationDelete('child-primary-4')).toBeNull())
    expect(loadPendingVoiceCalibrationDelete('child-primary-4')).toBeNull()
    expect(localStorage.getItem('hsc.voice-profile.v2.child-primary-4')).toBeNull()
    expect(await screen.findByText(/本机服务已确认声音特征不存在/)).toBeInTheDocument()
  })

  it('drops a late child A summary after switching to child B', async () => {
    const calibrationSummary = (childId: string) => ({
      child_id: childId, status: 'ready' as const, raw_audio_stored: false as const,
      profile: { ...serviceProfile, childId }, vocabulary: [childId === 'child-a' ? 'A词' : 'B词'], microphone,
    })
    let resolveChildA: ((value: ReturnType<typeof calibrationSummary>) => void) | undefined
    const childA = new Promise<ReturnType<typeof calibrationSummary>>((resolve) => { resolveChildA = resolve })
    vi.spyOn(localApi, 'voiceCalibrationSummary').mockImplementation((childId) => childId === 'child-a' ? childA : Promise.resolve(calibrationSummary(childId)))
    const view = render(<VoiceCalibrationPanel childId="child-a" parentManaged />)
    view.rerender(<VoiceCalibrationPanel childId="child-b" parentManaged />)
    expect(await screen.findByText(/B词/)).toBeInTheDocument()
    resolveChildA?.(calibrationSummary('child-a'))
    await Promise.resolve()
    expect(screen.getByText(/B词/)).toBeInTheDocument()
    expect(screen.queryByText(/A词/)).toBeNull()
  })
})
