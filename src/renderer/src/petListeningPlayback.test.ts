import { beforeEach, expect, it, vi } from 'vitest'
import { localApi, requestPetListeningAudio } from './api'
import { cancelLocalSpeech, queueLocalPcm } from './LocalInputs'
import { decodePetListening } from './petListeningAudio'
import { interruptPetListening, isPetListening, playPetListening } from './petListeningPlayback'
import type { PetMedia } from './petMedia'
import type { DiagnosticPromptResult } from './types'

vi.mock('./api', () => ({ localApi: { petPromptMedia: vi.fn(), petPlaybackActionReceipt: vi.fn(), petMediaControl: vi.fn(), petMediaPlayback: vi.fn(), petPcmControlReference: vi.fn() }, requestPetListeningAudio: vi.fn() }))
vi.mock('./LocalInputs', () => ({ queueLocalPcm: vi.fn(), cancelLocalSpeech: vi.fn() }))
vi.mock('./petListeningAudio', () => ({ decodePetListening: vi.fn() }))
const prompt = { prompt_id: 'p', item_version: 'v', child_id: 'c', diagnostic_item_id: 'i',
  item: { track: 'pet', answer_type: 'listening_text' } } as NonNullable<DiagnosticPromptResult['prompt']>
let row: PetMedia
function envelope() { return { media: structuredClone(row), local_only: true as const, cloud_transmission_allowed: false as const } }
beforeEach(() => {
  vi.resetAllMocks()
  row = { media_id: 'm', prompt_id: 'p', item_version: 'v', child_id: 'c', session_id: 's', diagnostic_item_id: 'i',
    kind: 'listening', source_version: 'source-v1', generation: 0, revision: 0, source_reviewed: true, status: 'ready',
    audio: { sha256: 'a'.repeat(64), frames: 16000, sample_rate: 16000 },
    qualification: { required_play_count: 2, task_complete_play_count: 0, task_delivery_qualified: false },
    local_only: true, cloud_transmission_allowed: false } as PetMedia
  vi.mocked(localApi.petPromptMedia).mockImplementation(async () => ({ prompt_id: 'p', item_version: 'v', child_id: 'c', session_id: 's',
    listening: [structuredClone(row)], speaking: [], selection_required: false, status: 'bound_media', local_only: true, cloud_transmission_allowed: false }))
  vi.mocked(localApi.petPlaybackActionReceipt).mockImplementation(async (_, __, key) => ({ child_id: 'c', session_id: 's', media_id: 'm', client_action_id: key,
    replay_decision: 'not_recorded', receipt: null, current: structuredClone(row), automatic_playback_allowed: false,
    local_only: true, cloud_transmission_allowed: false }))
  vi.mocked(localApi.petMediaControl).mockImplementation(async (_, command) => {
    row.generation++; row.revision++; row.status = command.command === 'pause' ? 'paused' : 'playing'
    return envelope()
  })
  vi.mocked(localApi.petMediaPlayback).mockImplementation(async (_, receipt) => {
    expect(Number.isInteger(receipt.monotonic_ms)).toBe(true)
    expect(receipt.expected_revision).toBe(row.revision)
    row.revision++
    if (receipt.event === 'ended') {
      row.status = 'playback_finished'
      row.qualification.task_complete_play_count!++
      row.qualification.task_delivery_qualified = row.qualification.task_complete_play_count === 2
    }
    return envelope()
  })
  vi.mocked(localApi.petPcmControlReference).mockImplementation(async () => ({version:'pet_full_audio_control_reference_v1',child_id:row.child_id,session_id:row.session_id,media_id:row.media_id,prompt_id:row.prompt_id,item_version:row.item_version,diagnostic_item_id:row.diagnostic_item_id,source_version:row.source_version,generation:row.generation,audio_sha256:row.audio.sha256!,spoken_text_sha256:'e'.repeat(64),source_review_event_id:'parent-review',local_only:true,cloud_transmission_allowed:false}))
  vi.mocked(requestPetListeningAudio).mockResolvedValue(new ArrayBuffer(44))
  vi.mocked(decodePetListening).mockResolvedValue({ pcm: new ArrayBuffer(32000), sampleRate: 16000 })
  vi.mocked(queueLocalPcm).mockImplementation(async (_, __, ___, start, options) => {
    if (options?.beforePlayback && !await options.beforePlayback(new AbortController().signal)) return 'superseded'
    start?.(); return 'finished'
  })
})
const run = (current = () => true, signal = new AbortController().signal) => playPetListening('s', prompt, 'action', current, signal)

it('requires authoritative school listening modality and keeps PET compatible', () => {
  const school = { ...prompt, item: { track: 'school', subject: '英语', answer_type: 'listening_text', pet_audio_modality: 'listening' } } as typeof prompt
  expect(isPetListening(school)).toBe(true)
  expect(isPetListening(prompt)).toBe(true)
  expect(isPetListening({ ...school, item: { ...school.item, pet_audio_modality: null } })).toBe(false)
  expect(isPetListening({ ...school, item: { ...school.item, subject: '数学' } })).toBe(false)
  expect(isPetListening({ ...school, item: { ...school.item, answer_type: 'text' } })).toBe(false)
})

it('plays school originals through the same exact media callback receipts', async () => {
  const school = { ...prompt, item: { track: 'school', subject: '英语', answer_type: 'listening_text', pet_audio_modality: 'listening' } } as typeof prompt
  expect((await playPetListening('s', school, 'school-action', () => true, new AbortController().signal)).status).toBe('finished')
  expect(queueLocalPcm).toHaveBeenCalledTimes(2)
  expect(vi.mocked(localApi.petMediaPlayback).mock.calls.map(call => call[1].event)).toEqual(['started', 'ended', 'started', 'ended'])
})

it.each(['lookup', 'download', 'decode'] as const)('interrupts %s before waiting for network or decoder completion', async phase => {
  let finish!: (value: any) => void
  const pending = new Promise<any>(resolve => { finish = resolve })
  if (phase === 'lookup') vi.mocked(localApi.petPromptMedia).mockReturnValueOnce(pending)
  if (phase === 'download') vi.mocked(requestPetListeningAudio).mockReturnValueOnce(pending)
  if (phase === 'decode') vi.mocked(decodePetListening).mockReturnValueOnce(pending)
  const promise = run()
  await vi.waitFor(() => expect(phase === 'lookup' ? localApi.petPromptMedia : phase === 'download' ? requestPetListeningAudio : decodePetListening).toHaveBeenCalled())
  expect(interruptPetListening('other-session')).toBe(false)
  expect(interruptPetListening('s')).toBe(true)
  expect((await promise).status).toBe('paused')
  finish(phase === 'download' ? new ArrayBuffer(44) : phase === 'decode' ? { pcm: new ArrayBuffer(32000), sampleRate: 16000 } : {})
  await Promise.resolve(); await Promise.resolve()
  expect(queueLocalPcm).not.toHaveBeenCalled()
  expect(localApi.petMediaControl).not.toHaveBeenCalled()
  if (phase === 'download') expect(vi.mocked(requestPetListeningAudio).mock.calls[0][3].aborted).toBe(true)
})

it('interrupts playing audio immediately and ignores a late renderer ended callback', async () => {
  let finish!: (value: 'finished') => void
  vi.mocked(queueLocalPcm).mockImplementation((_, __, ___, start) => {
    start?.(); return new Promise(resolve => { finish = resolve })
  })
  const promise = run()
  await vi.waitFor(() => expect(finish).toBeTypeOf('function'))
  expect(interruptPetListening('s')).toBe(true)
  expect(cancelLocalSpeech).toHaveBeenCalled()
  expect((await promise).status).toBe('paused')
  finish('finished')
  await Promise.resolve(); await Promise.resolve()
  expect(vi.mocked(localApi.petMediaPlayback).mock.calls.every(call => call[1].event !== 'ended')).toBe(true)
  expect(row.qualification.task_complete_play_count).toBe(0)
})

it('plays the required originals twice and records real callbacks with distinct keys', async () => {
  const result = await run()
  expect(result.status).toBe('finished')
  expect(queueLocalPcm).toHaveBeenCalledTimes(2)
  const requests = vi.mocked(localApi.petMediaPlayback).mock.calls.map(x => x[1])
  expect(requests.map(x => x.event)).toEqual(['started', 'ended', 'started', 'ended'])
  expect(new Set(requests.map(x => x.client_action_id)).size).toBe(4)
  expect(result.media?.qualification.task_delivery_qualified).toBe(true)
})

it('does not record ended until the renderer finishes', async () => {
  let finish!: (value: 'finished') => void
  row.qualification.required_play_count = 1
  vi.mocked(queueLocalPcm).mockImplementation((_, __, ___, start) => { start?.(); return new Promise(resolve => { finish = resolve }) })
  const promise = run()
  await vi.waitFor(() => expect(finish).toBeTypeOf('function'))
  expect(vi.mocked(localApi.petMediaPlayback).mock.calls.map(x => x[1].event)).toEqual(['started'])
  finish('finished')
  expect((await promise).status).toBe('finished')
})

it('settles only the original durable pending receipt without playing audio', async () => {
  const pending = { media_id: 'm', generation: 0, expected_revision: -1, client_action_id: 'old-ended',
    event: 'ended' as const, audio_sha256: row.audio.sha256!, frame_position: 16000, monotonic_ms: 2000 }
  row.status = 'pending_receipt'; row.receipt_retry_after_ms = 0; row.pending_playback_receipt = pending
  vi.mocked(localApi.petMediaPlayback).mockImplementation(async () => { row.status = 'playback_finished'; return envelope() })
  expect((await run()).status).toBe('finished')
  expect(localApi.petMediaPlayback).toHaveBeenCalledWith('s', pending)
  expect(queueLocalPcm).not.toHaveBeenCalled()
  expect(localApi.petMediaControl).not.toHaveBeenCalled()
})

it('does not turn cancellation into a completed listening receipt', async () => {
  vi.mocked(queueLocalPcm).mockImplementation(async (_, __, ___, start) => { start?.(); return 'cancelled' })
  expect((await run()).status).toBe('paused')
  expect(vi.mocked(localApi.petMediaPlayback).mock.calls.map(x => x[1].event)).toEqual(['started'])
  expect(row.status).toBe('paused')
})

it('rejects crossed children and missing or unreviewed source without synthesis', async () => {
  row.child_id = 'other'
  await expect(run()).rejects.toThrow('BINDING_CHANGED')
  row.child_id = 'c'; row.source_reviewed = false
  expect((await run()).status).toBe('unavailable')
  expect(requestPetListeningAudio).not.toHaveBeenCalled()
  expect(queueLocalPcm).not.toHaveBeenCalled()
})

it('checks original ownership again after binary fetch before device playback', async () => {
  let current = true
  vi.mocked(requestPetListeningAudio).mockImplementation(async () => { current = false; return new ArrayBuffer(44) })
  await expect(run(() => current)).rejects.toThrow('CANCELLED')
  expect(localApi.petMediaControl).not.toHaveBeenCalled()
  expect(queueLocalPcm).not.toHaveBeenCalled()
})

it('does not replay a completed original action after losing the Agent completion response', async () => {
  row.status = 'playback_finished'; row.qualification.task_complete_play_count = 2
  vi.mocked(localApi.petPlaybackActionReceipt).mockImplementation(async (_, __, key) => ({ child_id: 'c', session_id: 's', media_id: 'm', client_action_id: key,
    replay_decision: 'reuse_completed', receipt: { generation: 0, completion_accepted: true, request: {} as never },
    current: row, automatic_playback_allowed: false, local_only: true, cloud_transmission_allowed: false }))
  expect((await run()).status).toBe('finished')
  expect(queueLocalPcm).not.toHaveBeenCalled()
  expect(localApi.petMediaControl).not.toHaveBeenCalled()
})

it('requires explicit recovery if the same original action started but has no ended receipt', async () => {
  vi.mocked(localApi.petPlaybackActionReceipt).mockImplementation(async (_, __, key) => ({ child_id: 'c', session_id: 's', media_id: 'm', client_action_id: key,
    replay_decision: key.endsWith('started') ? 'await_recovery' : 'not_recorded', receipt: null,
    current: row, automatic_playback_allowed: false, local_only: true, cloud_transmission_allowed: false }))
  expect((await run()).status).toBe('paused')
  expect(queueLocalPcm).not.toHaveBeenCalled()
  expect(localApi.petMediaControl).not.toHaveBeenCalled()
})

it.each(['review', 'source', 'revision', 'generation', 'hash', 'frames', 'rate', 'child', 'status', 'selection', 'privacy'])(
  'rejects a changed %s at actual queue admission without playback receipts', async change => {
    let admission: ((signal: AbortSignal) => Promise<boolean>) | undefined
    let start: (() => void) | undefined
    let finish!: (result: 'superseded') => void
    vi.mocked(queueLocalPcm).mockImplementation((_, __, ___, callback, options) => {
      admission = options?.beforePlayback; start = callback
      return new Promise(resolve => { finish = resolve })
    })
    const pending = run()
    await vi.waitFor(() => expect(queueLocalPcm).toHaveBeenCalled())
    expect(admission).toBeTypeOf('function')
    if (change === 'review') row.source_reviewed = false
    if (change === 'source') row.source_version = 'source-v2'
    if (change === 'revision') row.revision++
    if (change === 'generation') row.generation++
    if (change === 'hash') row.audio.sha256 = 'b'.repeat(64)
    if (change === 'frames') row.audio.frames = 32000
    if (change === 'rate') row.audio.sample_rate = 24000
    if (change === 'child') row.child_id = 'other'
    if (change === 'status') row.status = 'paused'
    if (change === 'privacy') (row as { cloud_transmission_allowed: boolean }).cloud_transmission_allowed = true
    if (change === 'selection') vi.mocked(localApi.petPromptMedia).mockResolvedValueOnce({
      prompt_id: 'p', item_version: 'v', child_id: 'c', session_id: 's', listening: [row, row], speaking: [],
      selection_required: true, status: 'bound_media', local_only: true, cloud_transmission_allowed: false })
    expect(await admission!(new AbortController().signal)).toBe(false)
    finish('superseded')
    expect((await pending).status).toBe('unavailable')
    expect(localApi.petMediaPlayback).not.toHaveBeenCalled()
    expect(start).toBeTypeOf('function')
  })

it('cancels only its own queued audio key', async () => {
  vi.mocked(queueLocalPcm).mockImplementation(() => new Promise(() => undefined))
  const pending = run()
  await vi.waitFor(() => expect(queueLocalPcm).toHaveBeenCalled())
  interruptPetListening('s')
  expect((await pending).status).toBe('paused')
  expect(vi.mocked(cancelLocalSpeech).mock.calls.every(args => args[0] === 'action:pet:audio')).toBe(true)
  expect(localApi.petMediaPlayback).not.toHaveBeenCalled()
})

it('rejects audio changed by the resume response instead of qualifying old decoded bytes', async () => {
  vi.mocked(localApi.petMediaControl).mockImplementationOnce(async () => {
    row.status = 'playing'; row.generation++; row.revision++; row.audio.sha256 = 'b'.repeat(64)
    return envelope()
  })
  await expect(run()).rejects.toThrow('SOURCE_CHANGED')
  expect(queueLocalPcm).not.toHaveBeenCalled()
  expect(localApi.petMediaPlayback).not.toHaveBeenCalled()
})

it('fails closed on the admission read and permits a fresh explicit request afterward', async () => {
  vi.mocked(localApi.petPromptMedia).mockImplementationOnce(async () => ({ prompt_id: 'p', item_version: 'v', child_id: 'c', session_id: 's',
    listening: [structuredClone(row)], speaking: [], selection_required: false, status: 'bound_media', local_only: true, cloud_transmission_allowed: false }))
    .mockRejectedValueOnce(new Error('synthetic unavailable'))
  expect((await run()).status).toBe('unavailable')
  expect(localApi.petMediaPlayback).not.toHaveBeenCalled()
  expect((await playPetListening('s', prompt, 'fresh-explicit-action', () => true, new AbortController().signal)).status).toBe('finished')
  expect(vi.mocked(localApi.petMediaPlayback).mock.calls.every(call => call[1].client_action_id.startsWith('fresh-explicit-action'))).toBe(true)
})

it('does not play approved external PCM without a verified full-audio reference', async()=>{
 vi.mocked(localApi.petPcmControlReference).mockRejectedValue(new Error('FULL_REFERENCE_UNAVAILABLE'))
 const result=await run();expect(result.status).toBe('unavailable');expect(queueLocalPcm).not.toHaveBeenCalled();expect(localApi.petMediaPlayback).not.toHaveBeenCalled()
})
it('keeps the canonical media full-reference capsule private and carries it through the queue', async()=>{
 await run();const options=vi.mocked(queueLocalPcm).mock.calls[0][4]!
 expect(options.pcmControlReference).toMatchObject({prompt_id:'p',diagnostic_item_id:'i',source_review_event_id:'parent-review'})
 expect(options.pcmControlReference).not.toHaveProperty('stimulus_text');expect(options.pcmControlReference).not.toHaveProperty('text')
})
it('refuses a latest approval change before any native start', async()=>{
 let calls=0;const original=vi.mocked(localApi.petPcmControlReference).getMockImplementation()!
 vi.mocked(localApi.petPcmControlReference).mockImplementation(async(...args)=>({...await original(...args),source_review_event_id:++calls===1?'parent-review':'new-review'}))
 const result=await run();expect(result.status).toBe('unavailable');expect(localApi.petMediaPlayback).not.toHaveBeenCalled()
})
