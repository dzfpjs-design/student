import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { localApi } from './api'
import type { TutorBlackboardPresentation, TutorPlaybackCheckpoint } from './types'
import { clearTutorPlaybackLocalState, TutorPlaybackController } from './tutorPlaybackController'

const board: TutorBlackboardPresentation = { version: 'tutor_blackboard_v1', source: 'model_structured',
  sync_mode: 'segment_estimate', subject: '数学', title: '过程', segments: [1, 2].map(i => ({
    segment_id: `s${i}`, narration: `第${i}段`, duration_hint_ms: 1, blocks: [{ kind: 'text', text: `步骤${i}`, tone: 'chalk' }],
  })) }
let state: TutorPlaybackCheckpoint | null
let controller: TutorPlaybackController
let ports: ConstructorParameters<typeof TutorPlaybackController>[2]
let loseEnd = false
const receipts = new Map<string, TutorPlaybackCheckpoint>()
const tick = (): Promise<void> => new Promise(resolve => setTimeout(resolve, 0))

beforeEach(() => {
  state = null; loseEnd = false; receipts.clear(); localStorage.clear()
  ports = { speak: vi.fn(), cancel: vi.fn(), allowed: () => true, display: vi.fn(), error: vi.fn(), localNarration: vi.fn() }
  vi.spyOn(localApi, 'tutorPlayback').mockImplementation(async () => ({ playback: state }))
  vi.spyOn(localApi, 'mutateTutorPlayback').mockImplementation(async (_session, operation, values) => {
    const key = String(values.client_action_id)
    if (receipts.has(key)) return { playback: state! }
    if (operation === 'begin') state = { playback_id: 'p1', turn_id: String(values.turn_id), child_id: 'child', session_id: 'session',
      source_version: 'sv1', presentation_version: 'pv1', generation: 1, revision: 1, status: 'paused', segment_index: 0,
      segment_started: false, reason: null, presentation: board, evidence_kind: 'playback_only', replayed: false }
    else {
      if (!state || values.expected_revision !== state.revision || values.generation !== state.generation) throw new Error('CONFLICT')
      if (operation === 'control') state = { ...state, revision: state.revision + 1, generation: state.generation + 1,
        status: values.command === 'resume' ? 'playing' : values.command === 'cancel' ? 'cancelled' : 'paused', segment_started: false,
        segment_index: values.command === 'previous' ? Math.max(0, state.segment_index - 1) : values.command === 'restart' ? 0 : state.segment_index }
      else state = { ...state, revision: state.revision + 1, segment_started: values.event === 'started',
        segment_index: state.segment_index + Number(values.event === 'ended'),
        status: values.event === 'ended' && state.segment_index === 1 ? 'playback_finished' : 'playing' }
    }
    receipts.set(key, state!)
    if (loseEnd && operation === 'segment' && values.event === 'ended') { loseEnd = false; throw new Error('lost response') }
    return { playback: state! }
  })
  controller = new TutorPlaybackController('session', 'child', ports)
})
afterEach(() => { controller.dispose(); vi.restoreAllMocks(); localStorage.clear() })

it('an interrupt with no owned board does not turn a failed lookup into failure of a new plain reply', async () => {
  vi.mocked(localApi.tutorPlayback).mockRejectedValueOnce(new Error('offline lookup'))
  controller.interrupt()
  await tick(); await tick()
  expect(ports.cancel).toHaveBeenCalledTimes(1)
  expect(ports.error).not.toHaveBeenCalled()
  expect(localApi.mutateTutorPlayback).not.toHaveBeenCalled()
})

it('a late failed presentation lookup cannot cancel or report an error after its playback lease was interrupted', async () => {
  let reject!: (error: Error) => void
  vi.mocked(localApi.tutorPlayback).mockReturnValueOnce(new Promise((_resolve, fail) => { reject = fail }))
  const presenting = controller.present('old-turn', board, { localOnly: true }).catch(() => undefined)
  await tick()
  controller.interrupt()
  const cancellations = vi.mocked(ports.cancel).mock.calls.length
  reject(new Error('late lookup failure'))
  await presenting; await tick()
  expect(ports.cancel).toHaveBeenCalledTimes(cancellations)
  expect(ports.error).not.toHaveBeenCalled()
  expect(localApi.mutateTutorPlayback).not.toHaveBeenCalled()
})

it('advances only after actual segment completion has been durably acknowledged', async () => {
  await controller.present('turn', board, { localOnly: true })
  expect(ports.localNarration).toHaveBeenCalledExactlyOnceWith(true)
  const first = vi.mocked(ports.speak).mock.calls[0]
  first[2]('managed')
  await tick()
  expect(ports.speak).toHaveBeenCalledTimes(1)
  expect(state?.segment_index).toBe(0)
  first[3]()
  first[4].onPlaybackEnd?.()
  await tick(); await tick()
  expect(ports.speak).toHaveBeenCalledTimes(2)
  expect(ports.localNarration).toHaveBeenCalledTimes(1)
  expect(state?.segment_index).toBe(1)
  expect(vi.mocked(localApi.mutateTutorPlayback).mock.calls.map(call => call[2].event).filter(Boolean)).toEqual(['started', 'ended'])
  const second = vi.mocked(ports.speak).mock.calls[1]
  second[3](); second[4].onPlaybackEnd?.()
  await tick(); await tick()
  expect(state?.status).toBe('playback_finished')
  expect(vi.mocked(ports.localNarration!).mock.calls).toEqual([[true],[false]])
  expect(state?.evidence_kind).toBe('playback_only')
})

it('restores without speaking and ignores callbacks after interruption', async () => {
  await controller.present('turn', board, { localOnly: true })
  const first = vi.mocked(ports.speak).mock.calls[0]
  first[3](); await tick()
  controller.interrupt(); await tick()
  first[4].onPlaybackEnd?.(); await tick()
  expect(state?.status).toBe('paused')
  expect(state?.segment_index).toBe(0)
  controller.dispose()
  controller = new TutorPlaybackController('session', 'child', ports)
  vi.mocked(ports.speak).mockClear()
  await controller.restore()
  expect(ports.speak).not.toHaveBeenCalled()
  await controller.command('resume', { playbackId: 'p1', sourceVersion: 'sv1' })
  expect(ports.speak).toHaveBeenCalledWith('第1段', 'session', expect.any(Function), expect.any(Function), expect.any(Object))
})

it('recovers a lost ended receipt with the original key, without skipping or replaying the next segment', async () => {
  await controller.present('turn', board, { localOnly: true })
  const first = vi.mocked(ports.speak).mock.calls[0]
  first[3](); await tick(); loseEnd = true
  first[4].onPlaybackEnd?.(); await tick(); await tick()
  expect(ports.error).toHaveBeenCalled()
  expect(ports.speak).toHaveBeenCalledTimes(1)
  const original = vi.mocked(localApi.mutateTutorPlayback).mock.calls.find(call => call[2].event === 'ended')!
  controller.dispose(); controller = new TutorPlaybackController('session', 'child', ports)
  await controller.restore()
  await controller.command('resume', { playbackId: 'p1', sourceVersion: 'sv1' })
  const replays = vi.mocked(localApi.mutateTutorPlayback).mock.calls.filter(call => call[2].event === 'ended')
  expect(replays).toEqual([original, original])
  expect(vi.mocked(ports.speak).mock.calls.at(-1)?.[0]).toBe('第2段')
})

it('rejects a command bound to another presentation before applying it', async () => {
  await controller.present('turn', board, { localOnly: true })
  await expect(controller.command('previous', { playbackId: 'old-turn', sourceVersion: 'sv1' })).rejects.toThrow('BINDING_CHANGED')
  expect(vi.mocked(localApi.mutateTutorPlayback).mock.calls.some(call => call[2].command === 'previous')).toBe(false)
})

it('keeps only the current text fallback visible without reporting audible completion', async () => {
  await controller.present('turn', board, { localOnly: true })
  vi.mocked(ports.speak).mock.calls[0][2]('text_only')
  await tick(); await tick()
  expect(state?.status).toBe('paused')
  expect(state?.segment_index).toBe(0)
  expect(state?.segment_started).toBe(false)
  expect(vi.mocked(ports.display).mock.calls.at(-1)?.[1]).toBe(1)
  expect(ports.speak).toHaveBeenCalledTimes(1)
})

it('starts a new playback admission when explicitly presenting a cancelled explanation again', async () => {
  await controller.present('turn', board, { localOnly: true })
  await controller.command('cancel')
  await controller.present('turn', board, { localOnly: true })
  const starts = vi.mocked(localApi.mutateTutorPlayback).mock.calls.filter(call => call[1] === 'begin')
  expect(starts).toHaveLength(2)
  expect(starts[0][2].client_action_id).not.toBe(starts[1][2].client_action_id)
  expect(state?.status).toBe('playing')
  expect(state?.segment_index).toBe(0)
})

it('pauses an expired queued lesson without stopping the other active voice or advancing its segment', async () => {
  await controller.present('turn', board, { localOnly: true })
  const speech = vi.mocked(ports.speak).mock.calls[0]
  vi.mocked(ports.cancel).mockClear()
  speech[4].onNotDelivered?.('expired')
  speech[3](); speech[4].onPlaybackEnd?.()
  await tick(); await tick()
  expect(state?.status).toBe('paused')
  expect(state?.segment_index).toBe(0)
  expect(state?.segment_started).toBe(false)
  expect(ports.cancel).not.toHaveBeenCalled()
  expect(vi.mocked(localApi.mutateTutorPlayback).mock.calls.some(call => call[2].event === 'ended')).toBe(false)
})

it('clears all sessions only for the deleted child and fences the active runtime', () => {
  localStorage.setItem('hsc-tutor-playback-pending-v1:child:old', '{}')
  localStorage.setItem('hsc-tutor-playback-pending-v1:other:old', '{}')
  const listener = vi.fn()
  window.addEventListener('hsc-tutor-playback-forget', listener)
  clearTutorPlaybackLocalState('child')
  expect(localStorage.getItem('hsc-tutor-playback-pending-v1:child:old')).toBeNull()
  expect(localStorage.getItem('hsc-tutor-playback-pending-v1:other:old')).toBe('{}')
  expect(listener).toHaveBeenCalledWith(expect.objectContaining({ detail: { childId: 'child' } }))
  window.removeEventListener('hsc-tutor-playback-forget', listener)
})

it('admits the next question teaching after interruption and fences old speech callbacks', async () => {
  await controller.present('old-question-turn', board, { localOnly: true })
  const oldSpeech = vi.mocked(ports.speak).mock.calls[0]
  controller.interrupt()
  await tick(); await tick()
  state = { ...state!, status: 'invalidated', generation: state!.generation + 1 }
  await controller.present('new-question-turn', board, { localOnly: true })
  expect(state?.turn_id).toBe('new-question-turn')
  expect(state?.status).toBe('playing')
  expect(ports.speak).toHaveBeenCalledTimes(2)
  oldSpeech[3]?.(); oldSpeech[4].onPlaybackEnd?.()
  await tick(); await tick()
  expect(state?.turn_id).toBe('new-question-turn')
  expect(state?.segment_index).toBe(0)
  expect(state?.segment_started).toBe(false)
})
