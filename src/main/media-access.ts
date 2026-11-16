export type CameraPermissionStatus = 'granted' | 'denied' | 'not_determined' | 'restricted' | 'unknown'

/** Normalizes Electron's read-only OS result without requesting permission. */
export function normalizeCameraPermissionStatus(value: unknown): CameraPermissionStatus {
  if (value === 'granted' || value === 'denied' || value === 'restricted') return value
  if (value === 'not-determined') return 'not_determined'
  return 'unknown'
}
