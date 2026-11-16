import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { DeviceAvailabilityStrip, availabilityFromEnumeration } from './DeviceAvailabilityStrip'
import { localApi } from './api'
import { mediaDeviceInfoHash } from './mediaDevices'
import type { DeviceBinding, DeviceState } from './types'

const originalMediaDevices = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices')
const l523 = { kind: 'videoinput' as const, deviceId: 'l523', groupId: 'room', label: 'Logitech L523' }
const documentCamera = { kind: 'videoinput' as const, deviceId: 'fic760', groupId: 'desk', label: 'Fic760xUsbCamera' }

const deviceStates: DeviceState[] = [
  { device_type: 'camera', status: 'ready', message: '', historical_binding: { label: 'Logitech L523', verified_at: '2026-08-27T09:00:00Z' } },
  { device_type: 'document_camera', status: 'ready', message: '', historical_binding: { label: 'Fic760xUsbCamera', verified_at: '2026-08-27T09:00:01Z' } },
  { device_type: 'microphone', status: 'ready', message: '可用' },
]

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  if (originalMediaDevices) Object.defineProperty(navigator, 'mediaDevices', originalMediaDevices)
  else Reflect.deleteProperty(navigator, 'mediaDevices')
  delete window.desktop
})

describe('DeviceAvailabilityStrip', () => {
  it('keeps child-facing device status passive while still refreshing automatically', async () => {
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
      enumerateDevices: vi.fn().mockResolvedValue([l523, documentCamera]),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    } })
    const bindings = vi.spyOn(localApi, 'deviceBindings').mockResolvedValue([])

    render(<DeviceAvailabilityStrip devices={deviceStates} onRebind={vi.fn()} interactive={false} />)

    await waitFor(() => expect(bindings).toHaveBeenCalledOnce())
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
    expect(screen.getByText('高拍仪')).toBeInTheDocument()
    expect(screen.queryByText('观察摄像头')).not.toBeInTheDocument()
  })

  it('reports the unplugged highcam without exposing an online observation camera or opening video', async () => {
    const bindings: DeviceBinding[] = [
      { device_type: 'camera', system_id_hash: await mediaDeviceInfoHash({ ...l523, deviceId: 'previous-electron-profile-l523' }), label: 'Logitech L523', capabilities: {}, verified_at: '2026-08-27T09:00:00Z', updated_at: '' },
      { device_type: 'document_camera', system_id_hash: await mediaDeviceInfoHash(documentCamera), label: 'Fic760xUsbCamera', capabilities: {}, verified_at: '2026-08-27T09:00:01Z', updated_at: '' },
    ]
    const enumerateDevices = vi.fn().mockResolvedValue([l523])
    const getUserMedia = vi.fn()
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { enumerateDevices, getUserMedia, addEventListener: vi.fn(), removeEventListener: vi.fn() } })
    vi.spyOn(localApi, 'deviceBindings').mockResolvedValue(bindings)
    const rebind = vi.fn()

    render(<DeviceAvailabilityStrip devices={deviceStates} onRebind={rebind} />)

    expect(await screen.findByText(/当前系统未枚举到已绑定设备；历史绑定\/验证：Fic760xUsbCamera/)).toBeInTheDocument()
    expect(screen.queryByText(/Logitech L523|观察摄像头/)).not.toBeInTheDocument()
    expect(getUserMedia).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '重新绑定高拍仪' }))
    expect(rebind).toHaveBeenCalledOnce()
  })

  it('reports blank labels as permission-unknown instead of pretending a bound device is offline', async () => {
    const binding: DeviceBinding = { device_type: 'document_camera', system_id_hash: 'a'.repeat(64), label: documentCamera.label, capabilities: {}, verified_at: '', updated_at: '' }
    await expect(availabilityFromEnumeration(binding, [{ ...documentCamera, label: '' }])).resolves.toEqual({ availability: 'permission_unknown' })
  })

  it('only displays busy when the current session recorded an actual open failure', async () => {
    const binding: DeviceBinding = { device_type: 'document_camera', system_id_hash: 'a'.repeat(64), label: documentCamera.label, capabilities: {}, verified_at: '', updated_at: '' }
    await expect(availabilityFromEnumeration(binding, [documentCamera], {
      error_code: 'DEVICE_BUSY_OR_UNAVAILABLE', message: '被占用', occurred_at: '2026-08-28T00:00:00Z'
    })).resolves.toEqual({ availability: 'busy' })
  })

  it('refreshes after a devicechange and preserves a visible retry when enumeration temporarily fails', async () => {
    const binding: DeviceBinding = { device_type: 'document_camera', system_id_hash: await mediaDeviceInfoHash(documentCamera), label: documentCamera.label, capabilities: {}, verified_at: '', updated_at: '' }
    const listeners = new Map<string, () => void>()
    const enumerateDevices = vi.fn().mockResolvedValueOnce([documentCamera]).mockRejectedValueOnce(new Error('enumeration unavailable')).mockResolvedValueOnce([l523, documentCamera])
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
      enumerateDevices, addEventListener: vi.fn((name: string, listener: () => void) => listeners.set(name, listener)), removeEventListener: vi.fn(),
    } })
    vi.spyOn(localApi, 'deviceBindings').mockResolvedValue([binding])
    render(<DeviceAvailabilityStrip devices={deviceStates} onRebind={vi.fn()} />)
    await screen.findByText(/当前已枚举：Fic760xUsbCamera/)

    listeners.get('devicechange')?.()
    expect(await screen.findByText(/当前无法读取绑定资料或设备列表，在线状态未知/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '刷新设备在线状态' }))
    await waitFor(() => expect(screen.getByText(/当前已枚举：Fic760xUsbCamera/)).toBeInTheDocument())
  })
})
