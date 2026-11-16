import { describe, expect, it, vi } from 'vitest'
import { LEGACY_VOICE_EVENT_TIMEOUT_MS, requestLegacyVoiceEvent, requestOwnedVoiceEvent, tutorRecoveryCommand } from './voiceJourneyRuntime'

describe('bounded legacy voice handoffs', () => {
  it.each([false, true])('rejects an unconfirmed result with claimed=%s and ignores a late receipt', async (claimed) => {
    vi.useFakeTimers()
    let finish: ((handled: boolean) => void) | undefined
    const listener = (event: Event) => {
      if (claimed) event.preventDefault()
      finish = (event as CustomEvent).detail.resolve
    }
    if (claimed) window.addEventListener('legacy-test', listener)
    try {
      const result = requestLegacyVoiceEvent('legacy-test', {}).catch((error: Error) => error)
      await vi.advanceTimersByTimeAsync(LEGACY_VOICE_EVENT_TIMEOUT_MS)
      expect(await result).toMatchObject({ name: 'VoiceActionTimeoutError' })
      finish?.(true)
      expect(vi.getTimerCount()).toBe(0)
    } finally { window.removeEventListener('legacy-test', listener); vi.useRealTimers() }
  })
  it('accepts the legacy asynchronous receipt without requiring preventDefault', async () => {
    const listener = (event: Event) => { queueMicrotask(() => (event as CustomEvent).detail.resolve(true)) }
    window.addEventListener('legacy-receipt', listener)
    try { expect(await requestLegacyVoiceEvent('legacy-receipt', {})).toBe(true) }
    finally { window.removeEventListener('legacy-receipt', listener) }
  })
  it('cancels the wait and clears the deadline on privacy stop', async () => {
    vi.useFakeTimers()
    try {
      const controller = new AbortController()
      const result = requestLegacyVoiceEvent('legacy-abort', {}, controller.signal)
      controller.abort()
      await expect(result).rejects.toMatchObject({ name: 'AbortError' })
      expect(vi.getTimerCount()).toBe(0)
    } finally { vi.useRealTimers() }
  })
})

describe('owned voice handoffs', () => {
  it('settles immediately when the current screen does not own the action', async () => {
    expect(await requestOwnedVoiceEvent('missing-voice-owner', {})).toBe(false)
  })
  it('waits for an accepted asynchronous action rather than guessing completion from a timer', async () => {
    let finish!: (handled: boolean) => void
    const handler = (event: Event) => {
      event.preventDefault()
      finish = (event as CustomEvent<{ resolve: typeof finish }>).detail.resolve
    }
    window.addEventListener('owned-voice-action', handler)
    try {
      const settled = vi.fn()
      const pending = requestOwnedVoiceEvent('owned-voice-action', {}).then(settled)
      await Promise.resolve()
      expect(settled).not.toHaveBeenCalled()
      finish(true)
      await pending
      expect(settled).toHaveBeenCalledExactlyOnceWith(true)
    } finally { window.removeEventListener('owned-voice-action', handler) }
  })
  it('aborts an accepted action wait and ignores its late receipt', async () => {
    let finish!: (handled: boolean) => void
    const handler = (event: Event) => {
      event.preventDefault()
      finish = (event as CustomEvent<{ resolve: typeof finish }>).detail.resolve
    }
    window.addEventListener('cancelled-voice-action', handler)
    try {
      const controller = new AbortController()
      const pending = requestOwnedVoiceEvent('cancelled-voice-action', {}, controller.signal)
      controller.abort()
      await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
      finish(true)
    } finally { window.removeEventListener('cancelled-voice-action', handler) }
  })
})

describe('explicit tutor recovery commands', () => {
  it.each(['重试讲解', '重试本题。', '再试一次！'])('accepts an explicit retry: %s', (text) => {
    expect(tutorRecoveryCommand(text)).toBe('retry')
  })
  it.each(['今天老师让我们保留问题明天重新问', '这道题需要再试一次代入法', '我没有说确认再次请求', '重新问老师这份英语作业怎么做'])('does not swallow ordinary expression: %s', (text) => {
    expect(tutorRecoveryCommand(text)).toBeNull()
  })
})
