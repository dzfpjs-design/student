import { describe, expect, it } from 'vitest'
import { normalizeCameraPermissionStatus } from './media-access'

describe('camera permission adapter', () => {
  it('only normalizes the main-process read-only access result', () => {
    expect(normalizeCameraPermissionStatus('granted')).toBe('granted')
    expect(normalizeCameraPermissionStatus('not-determined')).toBe('not_determined')
    expect(normalizeCameraPermissionStatus('restricted')).toBe('restricted')
    expect(normalizeCameraPermissionStatus('not-an-electron-status')).toBe('unknown')
  })
})
