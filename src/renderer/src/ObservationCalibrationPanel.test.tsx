import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { mediaDeviceInfoHash } from './mediaDevices'
import { ObservationCalibrationPanel } from './ObservationCalibrationPanel'
import type { DeviceBinding, ObservationCalibrationRun } from './types'

const originalMediaDevices = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices')

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  if (originalMediaDevices) Object.defineProperty(navigator, 'mediaDevices', originalMediaDevices)
  else Reflect.deleteProperty(navigator, 'mediaDevices')
})

describe('ObservationCalibrationPanel', () => {
  it('opens only the bound camera and persists a passed local frame before releasing it', async () => {
    const device = {
      deviceId: 'calibration-camera-id', groupId: 'calibration-camera-group',
      kind: 'videoinput', label: 'L523', toJSON: () => ({}),
    } as MediaDeviceInfo
    const binding: DeviceBinding = {
      device_type: 'camera', system_id_hash: await mediaDeviceInfoHash({ ...device, deviceId: 'previous-electron-profile-camera-id' }), label: 'L523',
      capabilities: {}, verified_at: '2026-08-27T12:00:00Z', updated_at: '2026-08-27T12:00:00Z',
    }
    const stop = vi.fn()
    const track = { stop, addEventListener: vi.fn() } as unknown as MediaStreamTrack
    const stream = {
      getTracks: () => [track], getVideoTracks: () => [track],
    } as unknown as MediaStream
    const getUserMedia = vi.fn().mockResolvedValue(stream)
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { enumerateDevices: vi.fn().mockResolvedValue([device]), getUserMedia },
    })
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue()
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: vi.fn() } as unknown as CanvasRenderingContext2D)
    vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue('data:image/jpeg;base64,frame-data')

    const active: ObservationCalibrationRun = {
      run_id: 'calibration-run-001', child_id: 'child-primary-4', status: 'active', result: {},
      failure_code: null, started_at: '2026-08-27T12:00:00Z', completed_at: null, current_binding: true,
    }
    const passed: ObservationCalibrationRun = {
      ...active, status: 'passed', completed_at: '2026-08-27T12:01:00Z',
      result: {
        calibration_ready: true,
        scene_quality: { status: 'ready', message: '取景清晰。' },
        person_visible: true, person_message: '检测到正面人物。',
        metrics: { brightness: 112, sharpness: 90, width: 320, height: 180 },
        model_version: 'opencv_calibration_v1', raw_frame_stored: false,
      },
    }
    vi.spyOn(localApi, 'observationCalibrationSummary')
      .mockResolvedValueOnce({ latest: null, active: null, recent_runs: [] })
      .mockResolvedValue({ latest: passed, active: null, recent_runs: [passed] })
    vi.spyOn(localApi, 'startObservationCalibration').mockResolvedValue(active)
    const finish = vi.spyOn(localApi, 'finishObservationCalibration').mockResolvedValue(passed)

    render(<ObservationCalibrationPanel childId="child-primary-4" cameraBinding={binding} disabled={false} />)
    fireEvent.click(await screen.findByRole('button', { name: '打开座位取景' }))

    await waitFor(() => expect(getUserMedia).toHaveBeenCalledWith(expect.objectContaining({
      audio: false, video: expect.objectContaining({ deviceId: { exact: device.deviceId } }),
    })))
    const video = screen.getByLabelText('观察摄像头座位取景')
    Object.defineProperties(video, {
      readyState: { configurable: true, value: HTMLMediaElement.HAVE_CURRENT_DATA },
      videoWidth: { configurable: true, value: 640 },
      videoHeight: { configurable: true, value: 360 },
    })
    fireEvent.click(await screen.findByRole('button', { name: '检查当前取景' }))

    await waitFor(() => expect(finish).toHaveBeenCalledWith(active.run_id, 'frame-data'))
    expect(await screen.findByText(/当前座位取景通过/)).toBeInTheDocument()
    expect(screen.getByText('检测到正面人物')).toBeInTheDocument()
    expect(stop).toHaveBeenCalled()
  })

  it('ends an inherited active calibration without opening a camera', async () => {
    const active: ObservationCalibrationRun = {
      run_id: 'calibration-stale-001', child_id: 'child-junior-3', status: 'active', result: {},
      failure_code: null, started_at: '2026-08-27T12:00:00Z', completed_at: null, current_binding: true,
    }
    vi.spyOn(localApi, 'observationCalibrationSummary')
      .mockResolvedValueOnce({ latest: active, active, recent_runs: [active] })
      .mockResolvedValue({ latest: { ...active, status: 'cancelled' }, active: null, recent_runs: [] })
    const cancel = vi.spyOn(localApi, 'cancelObservationCalibration').mockResolvedValue({ ...active, status: 'cancelled' })

    render(<ObservationCalibrationPanel childId="child-junior-3" disabled={false} />)
    fireEvent.click(await screen.findByRole('button', { name: '结束遗留校准' }))

    await waitFor(() => expect(cancel).toHaveBeenCalledWith(active.run_id, 'PARENT_RECOVERED'))
    expect(await screen.findByText(/取景已中断/)).toBeInTheDocument()
  })

  it('keeps the concrete adjustment visible after a failed result is reloaded', async () => {
    const failed: ObservationCalibrationRun = {
      run_id: 'calibration-failed-001', child_id: 'child-primary-4', status: 'failed',
      failure_code: 'SCENE_TOO_DARK', started_at: '2026-08-27T12:00:00Z',
      completed_at: '2026-08-27T12:00:02Z', current_binding: true,
      result: {
        calibration_ready: false,
        scene_quality: { status: 'too_dark', message: '画面太暗，请打开正面照明，并把摄像头转向孩子常坐的位置。' },
        person_visible: false, person_message: '未检测到正面人物。',
        metrics: { brightness: 5, sharpness: 12, width: 320, height: 180 },
        model_version: 'opencv_calibration_v1', raw_frame_stored: false,
      },
    }
    vi.spyOn(localApi, 'observationCalibrationSummary').mockResolvedValue({ latest: failed, active: null, recent_runs: [failed] })

    render(<ObservationCalibrationPanel childId="child-primary-4" disabled={false} />)

    expect(await screen.findByText(/画面太暗，请打开正面照明/)).toBeInTheDocument()
    expect(screen.getByText('亮度 5')).toBeInTheDocument()
  })

  it('cancels a backend run that starts after the panel has already closed', async () => {
    const device = {
      deviceId: 'late-camera-id', groupId: 'late-camera-group', kind: 'videoinput',
      label: 'L523', toJSON: () => ({}),
    } as MediaDeviceInfo
    const binding: DeviceBinding = {
      device_type: 'camera', system_id_hash: await mediaDeviceInfoHash(device), label: 'L523',
      capabilities: {}, verified_at: '2026-08-27T12:00:00Z', updated_at: '2026-08-27T12:00:00Z',
    }
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { enumerateDevices: vi.fn().mockResolvedValue([device]), getUserMedia: vi.fn() },
    })
    let resolveStart: (run: ObservationCalibrationRun) => void = () => undefined
    vi.spyOn(localApi, 'observationCalibrationSummary').mockResolvedValue({ latest: null, active: null, recent_runs: [] })
    vi.spyOn(localApi, 'startObservationCalibration').mockImplementation(() => new Promise((resolve) => { resolveStart = resolve }))
    const cancel = vi.spyOn(localApi, 'cancelObservationCalibration').mockResolvedValue({} as ObservationCalibrationRun)
    const view = render(<ObservationCalibrationPanel childId="child-primary-4" cameraBinding={binding} disabled={false} />)
    fireEvent.click(await screen.findByRole('button', { name: '打开座位取景' }))
    view.unmount()
    resolveStart({
      run_id: 'late-run-001', child_id: 'child-primary-4', status: 'active', result: {},
      failure_code: null, started_at: '2026-08-27T12:00:00Z', completed_at: null, current_binding: true,
    })

    await waitFor(() => expect(cancel).toHaveBeenCalledWith('late-run-001', 'OPEN_SUPERSEDED'))
    expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled()
  })
})
