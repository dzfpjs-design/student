import { describe, expect, it } from 'vitest'
import { matchWakeWord, shouldReturnToWakeStandby } from './wakeWordRuntime'

describe('wake word runtime', () => {
  it('matches punctuation-insensitively and preserves the command after the wake word', () => {
    expect(matchWakeWord('小知，帮我讲一下这道题', '小知')).toEqual({ matched: true, command: '帮我讲一下这道题' })
    expect(matchWakeWord('小 知！', '小知')).toEqual({ matched: true, command: '' })
  })

  it('matches a complete Chinese wake word through local homophone ASR variants', () => {
    expect(matchWakeWord('小汁', '小知')).toEqual({ matched: true, command: '' })
    expect(matchWakeWord('小智，开始数学作业', '小知')).toEqual({ matched: true, command: '开始数学作业' })
  })

  it('does not send ordinary standby speech into the agent', () => {
    expect(matchWakeWord('我先把数学作业写完', '小知')).toEqual({ matched: false, command: '' })
  })

  it('returns to wake standby only after the configured conversation silence', () => {
    expect(shouldReturnToWakeStandby(true, 1_000, 60_999, 60)).toBe(false)
    expect(shouldReturnToWakeStandby(true, 1_000, 61_000, 60)).toBe(true)
    expect(shouldReturnToWakeStandby(false, 1_000, 90_000, 60)).toBe(false)
  })
})
