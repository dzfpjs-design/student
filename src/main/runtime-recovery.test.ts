import { describe, expect, it } from 'vitest'
import {
  BoundedRendererRecovery,
  RENDERER_RECOVERY_BASE_DELAY_MS,
  RENDERER_RECOVERY_MAX_ATTEMPTS,
  RENDERER_RECOVERY_WINDOW_MS,
} from './runtime-recovery'

describe('BoundedRendererRecovery', () => {
  it('backs off and stops a renderer crash loop', () => {
    const recovery = new BoundedRendererRecovery()
    const delays = Array.from({ length: RENDERER_RECOVERY_MAX_ATTEMPTS }, (_, index) => recovery.nextDelay(index))
    expect(delays[0]).toBe(RENDERER_RECOVERY_BASE_DELAY_MS)
    expect(delays).toEqual([...delays].sort((left, right) => Number(left) - Number(right)))
    expect(recovery.nextDelay(RENDERER_RECOVERY_MAX_ATTEMPTS)).toBeNull()
  })

  it('allows recovery again after a stable load or the failure window expires', () => {
    const recovery = new BoundedRendererRecovery()
    recovery.nextDelay(0)
    recovery.markStable()
    expect(recovery.nextDelay(1)).toBe(RENDERER_RECOVERY_BASE_DELAY_MS)

    const later = RENDERER_RECOVERY_WINDOW_MS + 2
    expect(recovery.nextDelay(later)).toBe(RENDERER_RECOVERY_BASE_DELAY_MS)
  })
})
