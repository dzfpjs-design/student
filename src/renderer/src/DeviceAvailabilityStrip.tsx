import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { FileScan, RefreshCw } from 'lucide-react'
import { localApi } from './api'
import { findBoundVideoDevice } from './mediaDevices'
import type { DeviceBinding, DeviceState } from './types'

type VideoDevice = Pick<MediaDeviceInfo, 'kind' | 'deviceId' | 'groupId' | 'label'>
type Availability = 'checking' | 'online' | 'offline' | 'permission_unknown' | 'busy' | 'permission_denied' | 'binding_unknown' | 'unbound'

type AvailabilityItem = {
  availability: Availability
  matchedLabel?: string
}

export async function availabilityFromEnumeration(
  binding: DeviceBinding | undefined,
  devices: VideoDevice[] | null,
  openFailure?: DeviceState['open_failure']
): Promise<AvailabilityItem> {
  if (!binding) return { availability: 'unbound' }
  if (openFailure?.error_code === 'DEVICE_BUSY_OR_UNAVAILABLE') return { availability: 'busy' }
  if (openFailure?.error_code === 'MEDIA_PERMISSION_DENIED') return { availability: 'permission_denied' }
  if (!devices) return { availability: 'binding_unknown' }
  const videos = devices.filter((item) => item.kind === 'videoinput')
  if (videos.length > 0 && videos.every((item) => !item.label.trim())) {
    return { availability: 'permission_unknown' }
  }
  const matched = await findBoundVideoDevice(videos, binding.system_id_hash, binding.label, 'document_camera')
  return matched
    ? { availability: 'online', matchedLabel: matched.label || binding.label }
    : { availability: 'offline' }
}

export function DeviceAvailabilityStrip({ devices, onRebind, children, interactive = true }: {
  devices: DeviceState[]
  onRebind: () => void
  children?: ReactNode
  interactive?: boolean
}): React.JSX.Element {
  const bindingsRef = useRef<DeviceBinding[] | null>(null)
  const [item, setAvailability] = useState<AvailabilityItem>({ availability: 'checking' })
  const [refreshing, setRefreshing] = useState(false)
  const [reloadError, setReloadError] = useState<string | null>(null)
  const server = devices.find((device) => device.device_type === 'document_camera')

  const refresh = useCallback(async (): Promise<void> => {
    setRefreshing(true)
    setReloadError(null)
    const [bindingResult, permissionResult] = await Promise.allSettled([
      localApi.deviceBindings(),
      window.desktop?.getCameraPermissionStatus?.() ?? Promise.resolve<DesktopCameraPermissionStatus>('unknown'),
    ])
    const loadedBindings = bindingResult.status === 'fulfilled' && Array.isArray(bindingResult.value)
      ? bindingResult.value
      : null
    if (loadedBindings) bindingsRef.current = loadedBindings

    const permission = permissionResult.status === 'fulfilled' ? permissionResult.value : 'unknown'
    const media = navigator.mediaDevices
    let enumerated: VideoDevice[] | null = null
    if (media?.enumerateDevices) {
      try {
        enumerated = await media.enumerateDevices()
      } catch {
        // Enumerating does not request permission.  A failure means we cannot
        // distinguish an unplugged device from an unobservable device list.
        enumerated = null
      }
    }

    const activeBindings = loadedBindings ?? bindingsRef.current
    const bound = activeBindings?.find((binding) => binding.device_type === 'document_camera')
    const result: AvailabilityItem = bound
      ? permission === 'denied' || permission === 'restricted'
        ? { availability: 'permission_denied' }
        : await availabilityFromEnumeration(bound, enumerated, server?.open_failure)
      : { availability: server?.historical_binding ? 'binding_unknown' : 'unbound' }
    setAvailability(result)
    if (!loadedBindings) setReloadError('设备绑定资料暂未读取；在线状态不会被猜测。')
    setRefreshing(false)
  }, [server])

  useEffect(() => {
    void refresh()
  }, [refresh])

  useEffect(() => {
    const media = navigator.mediaDevices
    if (!media?.addEventListener) return
    const onDeviceChange = (): void => { void refresh() }
    media.addEventListener('devicechange', onDeviceChange)
    return () => media.removeEventListener('devicechange', onDeviceChange)
  }, [refresh])

  const shouldOfferRebind = item.availability === 'offline' || item.availability === 'unbound'

  return (
    <>
      <div className="device-item">
        <FileScan size={19} />
        <div>
          <strong>高拍仪</strong>
          <span>{availabilityMessage(item, server)}</span>
        </div>
        <i className={`status-dot status-${availabilityTone(item.availability)}`} />
      </div>
      {children}
      {(reloadError || interactive) && (
        <div className="device-status-actions" role="status">
          {reloadError && <span>{reloadError}</span>}
          {interactive && <button className="text-button" disabled={refreshing} onClick={() => void refresh()} aria-label="刷新设备在线状态">
            <RefreshCw size={15} />{refreshing ? '正在刷新' : '刷新设备状态'}
          </button>}
          {interactive && shouldOfferRebind && <button className="text-button" onClick={onRebind}>重新绑定高拍仪</button>}
        </div>
      )}
    </>
  )
}

function availabilityMessage(item: AvailabilityItem, server?: DeviceState): string {
  const historic = server?.historical_binding
    ? `历史绑定/验证：${server.historical_binding.label}。`
    : '尚无历史绑定。'
  if (item.availability === 'online') return `当前已枚举：${item.matchedLabel ?? historic}；${historic}应用当前未读取。`
  if (item.availability === 'offline') return `当前系统未枚举到已绑定设备；${historic}请检查连接后刷新或重新绑定。`
  if (item.availability === 'permission_unknown') return `设备标签尚不可见，当前在线状态未知；${historic}不会自动请求摄像头权限。`
  if (item.availability === 'busy') return `上次打开时提示设备被占用；${historic}请关闭其他程序后重试打开。`
  if (item.availability === 'permission_denied') return `系统拒绝了摄像头权限；${historic}请由家长在系统设置中允许后再重试。`
  if (item.availability === 'binding_unknown') return `当前无法读取绑定资料或设备列表，在线状态未知；${historic}`
  if (item.availability === 'unbound') return '尚未绑定；不会自动选择或打开任何视频设备。'
  return `正在本机检查设备列表；${historic}`
}

function availabilityTone(availability: Availability): 'ready' | 'unavailable' | 'paused' | 'error' {
  if (availability === 'online') return 'ready'
  if (availability === 'offline' || availability === 'unbound') return 'unavailable'
  if (availability === 'busy' || availability === 'permission_denied') return 'error'
  return 'paused'
}
