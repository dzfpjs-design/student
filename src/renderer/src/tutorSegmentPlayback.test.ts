import { describe, expect, it, vi } from 'vitest'
import { playTutorSegments } from './tutorSegmentPlayback'
import type { TutorBlackboardPresentation } from './types'

const board: TutorBlackboardPresentation = { version: 'tutor_blackboard_v1', source: 'model_structured', sync_mode: 'segment_estimate', subject: '数学', title: '分步讲解', segments: [
  { segment_id: 'one', narration: '先想第一步', blocks: [], duration_hint_ms: 1 },
  { segment_id: 'two', narration: '现在看第二步', blocks: [], duration_hint_ms: 1 },
] }

describe('audio-driven tutor segments', () => {
  it('never uses estimated duration or delivery receipt as the next segment trigger', async () => {
    const speak = vi.fn(), reveal = vi.fn(), complete = vi.fn()
    playTutorSegments({ presentation: board, sessionId: 's', speak, options: { localOnly: true }, isCurrent: () => true, onReveal: reveal, onComplete: complete })
    expect(speak).toHaveBeenCalledTimes(1)
    expect(reveal).not.toHaveBeenCalled()
    const first = speak.mock.calls[0]
    first[2]('system_offline')
    await Promise.resolve()
    expect(speak).toHaveBeenCalledTimes(1)
    first[3]()
    expect(reveal).toHaveBeenCalledWith(1)
    first[4].onPlaybackEnd()
    first[4].onPlaybackEnd()
    await Promise.resolve()
    expect(speak).toHaveBeenCalledTimes(2)
    expect(reveal).not.toHaveBeenCalledWith(2)
    speak.mock.calls[1][3]()
    speak.mock.calls[1][4].onPlaybackEnd()
    await Promise.resolve()
    expect(reveal).toHaveBeenLastCalledWith(2)
    expect(complete).toHaveBeenCalledOnce()
  })
  it('keeps only the current segment when voice is unavailable', async () => {
    const speak = vi.fn(), reveal = vi.fn()
    playTutorSegments({ presentation: board, sessionId: 's', speak, options: {}, isCurrent: () => true, onReveal: reveal })
    speak.mock.calls[0][2]('text_only')
    await Promise.resolve()
    expect(reveal).toHaveBeenCalledExactlyOnceWith(1)
    expect(speak).toHaveBeenCalledTimes(1)
  })
  it('does not reveal or resume after interruption, including a late OS end event', async () => {
    let current = true
    const speak = vi.fn(), reveal = vi.fn()
    playTutorSegments({ presentation: board, sessionId: 's', speak, options: {}, isCurrent: () => current, onReveal: reveal })
    current = false
    speak.mock.calls[0][3]()
    speak.mock.calls[0][4].onPlaybackEnd()
    await Promise.resolve()
    expect(reveal).not.toHaveBeenCalled()
    expect(speak).toHaveBeenCalledTimes(1)
  })
})
