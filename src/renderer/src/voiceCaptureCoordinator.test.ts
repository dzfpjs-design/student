import { beforeEach, describe, expect, it, vi } from 'vitest'
import { claimVoiceCapture, getVoiceCaptureOwner, releaseVoiceCapture, resetVoiceCaptureCoordinatorForTests, subscribeVoiceCapture } from './voiceCaptureCoordinator'

describe('voiceCaptureCoordinator', () => {
  beforeEach(() => resetVoiceCaptureCoordinatorForTests())

  it('allows one capture mode at a time and ignores release from a non-owner', () => {
    expect(claimVoiceCapture('hands-free')).toBe(true)
    expect(claimVoiceCapture('manual')).toBe(false)
    expect(getVoiceCaptureOwner()).toBe('hands-free')

    releaseVoiceCapture('manual')
    expect(getVoiceCaptureOwner()).toBe('hands-free')

    releaseVoiceCapture('hands-free')
    expect(getVoiceCaptureOwner()).toBeNull()
    expect(claimVoiceCapture('manual')).toBe(true)
  })

  it('treats a repeated claim by the owner as idempotent and notifies only on changes', () => {
    const listener = vi.fn()
    const unsubscribe = subscribeVoiceCapture(listener)

    expect(claimVoiceCapture('manual')).toBe(true)
    expect(claimVoiceCapture('manual')).toBe(true)
    releaseVoiceCapture('manual')

    expect(listener).toHaveBeenCalledTimes(2)
    unsubscribe()
    claimVoiceCapture('hands-free')
    expect(listener).toHaveBeenCalledTimes(2)
  })
})
