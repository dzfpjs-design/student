import { execFile } from 'node:child_process'
import { createHash } from 'node:crypto'
import { promisify } from 'node:util'

const execFileAsync = promisify(execFile)

export type VideoHardwareItem = {
  name: string
  manufacturer: string | null
  driver_version: string | null
  driver_provider: string | null
  driver_date: string | null
  usb_vid: string | null
  usb_pid: string | null
  transport_hint: 'usb' | 'bluetooth' | 'software' | 'unknown'
  instance_id_hash: string
}

export type VideoHardwareInventory = {
  supported: boolean
  source: 'windows_pnp_cim_v1' | 'unsupported_platform' | 'windows_inventory_failed'
  items: VideoHardwareItem[]
  error_code?: 'WINDOWS_INVENTORY_FAILED'
}

type RawWindowsVideoDevice = {
  name?: unknown
  manufacturer?: unknown
  instanceId?: unknown
  driverVersion?: unknown
  driverProvider?: unknown
  driverDate?: unknown
}

const WINDOWS_VIDEO_INVENTORY_SCRIPT = String.raw`
$devices = @(Get-PnpDevice -PresentOnly -Class Camera,Image -ErrorAction SilentlyContinue)
$result = foreach ($device in $devices) {
  $driver = @{}
  Get-PnpDeviceProperty -InstanceId $device.InstanceId -KeyName @(
    'DEVPKEY_Device_DriverVersion',
    'DEVPKEY_Device_DriverProvider',
    'DEVPKEY_Device_DriverDate'
  ) -ErrorAction SilentlyContinue | ForEach-Object { $driver[$_.KeyName] = $_.Data }
  [PSCustomObject]@{
    name = $device.FriendlyName
    manufacturer = $device.Manufacturer
    instanceId = $device.InstanceId
    driverVersion = $driver['DEVPKEY_Device_DriverVersion']
    driverProvider = $driver['DEVPKEY_Device_DriverProvider']
    driverDate = if ($driver['DEVPKEY_Device_DriverDate']) { $driver['DEVPKEY_Device_DriverDate'].ToString('o') } else { $null }
  }
}
ConvertTo-Json -InputObject @($result) -Compress -Depth 3
`.trim()

export async function collectVideoHardwareInventory(platform = process.platform): Promise<VideoHardwareInventory> {
  if (platform !== 'win32') return { supported: false, source: 'unsupported_platform', items: [] }
  try {
    const result = await execFileAsync('powershell.exe', [
      '-NoLogo', '-NoProfile', '-NonInteractive',
      '-Command', WINDOWS_VIDEO_INVENTORY_SCRIPT,
    ], { encoding: 'utf8', timeout: 15_000, windowsHide: true, maxBuffer: 1_000_000 })
    return {
      supported: true,
      source: 'windows_pnp_cim_v1',
      items: normalizeWindowsVideoInventory(JSON.parse(String(result.stdout || '').replace(/^\uFEFF/, '').trim() || '[]')),
    }
  } catch {
    return { supported: true, source: 'windows_inventory_failed', items: [], error_code: 'WINDOWS_INVENTORY_FAILED' }
  }
}

export function normalizeWindowsVideoInventory(input: unknown): VideoHardwareItem[] {
  const rows = Array.isArray(input) ? input : input && typeof input === 'object' ? [input] : []
  return rows.flatMap((candidate) => {
    if (!candidate || typeof candidate !== 'object') return []
    const row = candidate as RawWindowsVideoDevice
    const name = cleanString(row.name)
    const instanceId = cleanString(row.instanceId)
    if (!name || !instanceId) return []
    const vid = instanceId.match(/(?:^|\\|&)VID_([0-9A-F]{4})(?:&|\\|$)/i)?.[1]?.toUpperCase() ?? null
    const pid = instanceId.match(/(?:^|\\|&)PID_([0-9A-F]{4})(?:&|\\|$)/i)?.[1]?.toUpperCase() ?? null
    return [{
      name,
      manufacturer: cleanString(row.manufacturer),
      driver_version: cleanString(row.driverVersion),
      driver_provider: cleanString(row.driverProvider),
      driver_date: cleanString(row.driverDate),
      usb_vid: vid,
      usb_pid: pid,
      transport_hint: transportHint(instanceId),
      instance_id_hash: createHash('sha256').update(instanceId).digest('hex'),
    }]
  })
}

function cleanString(value: unknown): string | null {
  if (typeof value !== 'string') return null
  const cleaned = value.replace(/[\u0000-\u001f\u007f]/g, ' ').trim().slice(0, 240)
  return cleaned || null
}

function transportHint(instanceId: string): VideoHardwareItem['transport_hint'] {
  const prefix = instanceId.split('\\', 1)[0].toUpperCase()
  if (prefix === 'USB') return 'usb'
  if (prefix === 'BTHENUM' || prefix === 'BTHLEDEVICE') return 'bluetooth'
  if (prefix === 'SWD' || prefix === 'ROOT') return 'software'
  return 'unknown'
}
