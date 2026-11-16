import { describe, expect, it, vi } from 'vitest'
import {
  connectRequestedVideoDevices,
  deviceConnectSpeech,
  deviceConnectSucceeded,
  OBSERVATION_CAMERA_REMOVED,
  OBSERVATION_CAMERA_REMOVED_MESSAGE,
  type ConnectableVideoDevice,
} from './deviceConnectionRuntime'
import type { CameraDeviceOutput } from './cameraDeviceRuntime'
import type { DeviceBinding } from './types'

const binding = (device_type: ConnectableVideoDevice, label: string): DeviceBinding => ({
  device_type,
  label,
  system_id_hash: 'not-the-current-browser-hash',
  capabilities: {},
  verified_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
})

const media = (label: string, id: string): MediaDeviceInfo => ({
  kind: 'videoinput', label, deviceId: id, groupId: `group-${id}`, toJSON: () => ({}),
})

const connected = (device: ConnectableVideoDevice): CameraDeviceOutput => device === 'camera'
  ? { device_type: device, device_status: 'active', frame_confirmed: true, lease_confirmed: true, observation_status: 'enabled' }
  : { device_type: device, device_status: 'active', frame_confirmed: true, lease_confirmed: true, persisted_status: 'active' }

describe('spoken device connection runtime', () => {
  it('re-enumerates and connects only the bound document camera for a legacy all-devices request', async () => {
    const enumerate = vi.fn(async () => [media('观察摄像头', 'camera'), media('高拍仪', 'document')])
    const connect = vi.fn(async (_session: string, device: ConnectableVideoDevice) => connected(device))
    const result = await connectRequestedVideoDevices('session', 'all', new AbortController().signal, {
      bindings: async () => [binding('camera', '观察摄像头'), binding('document_camera', '高拍仪')],
      enumerate,
      connect,
      incident: vi.fn(),
    })
    expect(enumerate).toHaveBeenCalledOnce()
    expect(connect).toHaveBeenCalledExactlyOnceWith('session', 'document_camera', expect.any(AbortSignal))
    expect(deviceConnectSucceeded(result)).toBe(true)
    expect(deviceConnectSpeech(result)).toContain(OBSERVATION_CAMERA_REMOVED_MESSAGE)
    expect(deviceConnectSpeech(result)).toContain('高拍仪已经连接并收到画面')
  })

  it('never falls back to the observation camera when the bound document camera is missing', async () => {
    const incident = vi.fn(async () => undefined)
    const connect = vi.fn()
    const result = await connectRequestedVideoDevices('session', 'all', new AbortController().signal, {
      bindings: async () => [binding('camera', '观察摄像头'), binding('document_camera', '高拍仪')],
      enumerate: async () => [media('观察摄像头', 'camera')],
      connect,
      incident,
    })
    expect(result.devices).toEqual([
      { device_type: 'document_camera', status: 'failed', error_code: 'BOUND_DEVICE_NOT_FOUND' },
    ])
    expect(connect).not.toHaveBeenCalled()
    const speech = deviceConnectSpeech(result)
    expect(speech).toContain(OBSERVATION_CAMERA_REMOVED_MESSAGE)
    expect(speech).toContain('请把高拍仪的 USB 插头拔下再插紧')
    expect(speech).not.toContain('请把观察摄像头')
    expect(incident).toHaveBeenCalledWith('session', 'document_camera', 'BOUND_DEVICE_NOT_FOUND')
  })

  it('rejects an old observation-camera request before it can read bindings or touch media', async () => {
    const bindings = vi.fn()
    const enumerate = vi.fn()
    const connect = vi.fn()
    const incident = vi.fn()
    const result = await connectRequestedVideoDevices('session', 'camera', new AbortController().signal, {
      bindings, enumerate, connect, incident,
    })
    expect(result.devices).toEqual([{ device_type: 'camera', status: 'failed', error_code: OBSERVATION_CAMERA_REMOVED }])
    expect(deviceConnectSpeech(result)).toBe(OBSERVATION_CAMERA_REMOVED_MESSAGE)
    for (const sideEffect of [bindings, enumerate, connect, incident]) expect(sideEffect).not.toHaveBeenCalled()
  })

  it('distinguishes missing binding and device occupation from a physical replug', async () => {
    const notBound = await connectRequestedVideoDevices('session', 'document_camera', new AbortController().signal, {
      bindings: async () => [], enumerate: vi.fn(), connect: vi.fn(), incident: vi.fn(),
    })
    expect(deviceConnectSpeech(notBound)).toContain('请让家长帮忙设置一次')
    expect(deviceConnectSpeech(notBound)).not.toContain('拔下再插紧')

    const busy = await connectRequestedVideoDevices('session', 'document_camera', new AbortController().signal, {
      bindings: async () => [binding('document_camera', '高拍仪')],
      enumerate: async () => [media('高拍仪', 'document')],
      connect: async () => { throw new DOMException('busy', 'NotReadableError') },
      incident: vi.fn(),
    })
    expect(busy.devices[0]).toMatchObject({ status: 'failed', error_code: 'DEVICE_BUSY_OR_UNAVAILABLE' })
    expect(deviceConnectSpeech(busy)).toContain('被其他程序占用')
    expect(deviceConnectSpeech(busy)).not.toContain('拔下再插紧')
  })
})
