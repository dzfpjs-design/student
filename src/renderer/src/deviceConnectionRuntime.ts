import { localApi } from './api'
import { requestCameraDevice, type CameraDeviceOutput } from './cameraDeviceRuntime'
import { mediaDeviceFailure, resolveBoundVideoDevice } from './mediaDevices'
import type { DeviceBinding } from './types'

export type ConnectableVideoDevice = CameraDeviceOutput['device_type']
export type DeviceConnectTarget = ConnectableVideoDevice | 'all'
export const OBSERVATION_CAMERA_REMOVED = 'OBSERVATION_CAMERA_REMOVED'
export const OBSERVATION_CAMERA_REMOVED_MESSAGE = '学习伙伴已移除观察摄像头功能。'

export type DeviceConnectAttempt =
  | ({ status: 'connected' } & CameraDeviceOutput)
  | { device_type: ConnectableVideoDevice; status: 'failed'; error_code: string }

export type DeviceConnectResult = {
  requested_device: DeviceConnectTarget
  connected_at: string
  devices: DeviceConnectAttempt[]
}

type Dependencies = {
  bindings: () => Promise<DeviceBinding[]>
  enumerate: () => Promise<MediaDeviceInfo[]>
  connect: (sessionId: string, device: ConnectableVideoDevice, signal: AbortSignal) => Promise<CameraDeviceOutput>
  incident: (sessionId: string, device: ConnectableVideoDevice, code: string) => Promise<void>
}

const DEVICE_LABEL: Record<ConnectableVideoDevice, string> = {
  camera: '观察摄像头',
  document_camera: '高拍仪',
}

const SAFE_CONNECT_ERRORS = new Set([
  'DEVICE_NOT_BOUND', 'DEVICE_BINDING_UNAVAILABLE', 'BOUND_DEVICE_NOT_FOUND', 'MEDIA_API_UNAVAILABLE', 'MEDIA_PERMISSION_DENIED',
  'DEVICE_NOT_FOUND', 'DEVICE_BUSY_OR_UNAVAILABLE', 'UNSUPPORTED_DEVICE_MODE', 'DEVICE_OPEN_FAILED',
  'DEVICE_NO_CONFIRMED_FRAME', 'DEVICE_RESULT_UNCONFIRMED', 'DEVICE_RECEIVER_UNAVAILABLE',
])

function safeErrorCode(cause: unknown): string {
  const message = cause instanceof Error ? cause.message : ''
  if (SAFE_CONNECT_ERRORS.has(message)) return message
  const translated = mediaDeviceFailure(cause, '视频设备').code
  return SAFE_CONNECT_ERRORS.has(translated) ? translated : 'DEVICE_OPEN_FAILED'
}

const defaults: Dependencies = {
  bindings: () => localApi.deviceBindings(),
  enumerate: async () => {
    if (!navigator.mediaDevices?.enumerateDevices) throw new Error('MEDIA_API_UNAVAILABLE')
    return navigator.mediaDevices.enumerateDevices()
  },
  connect: (sessionId, device, signal) => requestCameraDevice(sessionId, device, 'resume', signal),
  incident: (sessionId, device, code) => localApi.recordDeviceIncident(
    sessionId, device, code, `${DEVICE_LABEL[device]}语音连接未完成。`
  ),
}

/** Re-enumerate the bound document camera only, including older requests for all devices. */
export async function connectRequestedVideoDevices(
  sessionId: string,
  requested: DeviceConnectTarget,
  signal: AbortSignal,
  dependencies: Dependencies = defaults,
): Promise<DeviceConnectResult> {
  const result = (attempt: DeviceConnectAttempt): DeviceConnectResult => ({
    requested_device: requested, connected_at: new Date().toISOString(), devices: [attempt],
  })
  if (signal.aborted) throw new DOMException('Device connection cancelled', 'AbortError')
  if (requested === 'camera') return result({ device_type: 'camera', status: 'failed', error_code: OBSERVATION_CAMERA_REMOVED })

  const device = 'document_camera'
  const failed = async (errorCode: string): Promise<DeviceConnectResult> => {
    if (signal.aborted) throw new DOMException('Device connection cancelled', 'AbortError')
    await Promise.resolve(dependencies.incident(sessionId, device, errorCode)).catch(() => undefined)
    return result({ device_type: device, status: 'failed', error_code: errorCode })
  }
  let binding: DeviceBinding | undefined
  try { binding = (await dependencies.bindings()).find((item) => item.device_type === device) }
  catch { return failed('DEVICE_BINDING_UNAVAILABLE') }
  if (!binding) return failed('DEVICE_NOT_BOUND')
  try {
    const available = await dependencies.enumerate()
    if (signal.aborted) throw new DOMException('Device connection cancelled', 'AbortError')
    const selected = await resolveBoundVideoDevice(available, binding.system_id_hash, binding.label, device)
    if (signal.aborted) throw new DOMException('Device connection cancelled', 'AbortError')
    if (!selected) return failed('BOUND_DEVICE_NOT_FOUND')
    return result({ status: 'connected', ...await dependencies.connect(sessionId, device, signal) })
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause
    return failed(safeErrorCode(cause))
  }
}

export function deviceConnectSpeech(result: DeviceConnectResult): string {
  if (result.requested_device === 'camera') return OBSERVATION_CAMERA_REMOVED_MESSAGE
  const response = result.devices.map((device) => {
    const label = DEVICE_LABEL[device.device_type]
    if (device.status === 'connected') return `${label}已经连接并收到画面`
    if (device.error_code === 'BOUND_DEVICE_NOT_FOUND' || device.error_code === 'DEVICE_NOT_FOUND') {
      return `电脑没有发现${label}，请把${label}的 USB 插头拔下再插紧，然后再说“连接${label}”`
    }
    if (device.error_code === 'DEVICE_NOT_BOUND') return `${label}还没有完成首次绑定，请让家长帮忙设置一次`
    if (device.error_code === 'DEVICE_BINDING_UNAVAILABLE') return `暂时读不到${label}的绑定记录，请让家长检查学习伙伴是否已经正常启动`
    if (device.error_code === 'MEDIA_PERMISSION_DENIED') return `${label}权限没有打开，请让家长在 Windows 隐私设置里允许摄像头权限`
    if (device.error_code === 'DEVICE_BUSY_OR_UNAVAILABLE') return `${label}可能被其他程序占用，请先关闭相机或会议软件，再让我重试`
    if (device.error_code === 'MEDIA_API_UNAVAILABLE') return `这台电脑暂时不能读取${label}，请让家长检查应用和系统摄像头设置`
    return `${label}在电脑里能找到，但还没有收到稳定画面；请先关闭可能占用它的程序，再让我重试`
  }).join('。') + '。'
  return result.requested_device === 'all' ? `${OBSERVATION_CAMERA_REMOVED_MESSAGE}${response}` : response
}

export function deviceConnectSucceeded(result: DeviceConnectResult): boolean {
  return result.devices.length > 0 && result.devices.every((item) => item.status === 'connected')
}
