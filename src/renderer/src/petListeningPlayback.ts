import { localApi, requestPetListeningAudio } from './api'
import { cancelLocalSpeech, queueLocalPcm } from './LocalInputs'
import { decodePetListening } from './petListeningAudio'
import { referenceMatchesMedia, samePcmReference } from './petPlaybackReference'
import type { PetMedia, PetPlaybackReceipt } from './petMedia'
import type { DiagnosticPromptResult } from './types'

type Prompt = NonNullable<DiagnosticPromptResult['prompt']>
export type ListeningPromptIdentity = Pick<Prompt, 'child_id' | 'prompt_id' | 'item_version' | 'diagnostic_item_id'>
export type PetListeningResult = { status: 'finished' | 'paused' | 'pending_receipt' | 'unavailable'; response: string; media?: PetMedia }
let playbackOwner = 0
let activeListening: { sessionId: string; mediaId: string | null; owner: number; cancel: () => void } | null = null

export function interruptPetListening(sessionId: string): boolean {
  if (!activeListening || activeListening.sessionId !== sessionId) return false
  activeListening.cancel()
  return true
}

export function stopPetListening(sessionId: string, mediaId: string): boolean {
  if (!activeListening || activeListening.sessionId !== sessionId || activeListening.mediaId !== mediaId) return false
  activeListening.cancel()
  return true
}

export function isPetListening(prompt: Pick<Prompt, 'item'>): boolean {
  return (prompt.item.track === 'pet' && (prompt.item.pet_audio_modality === 'listening' || prompt.item.answer_type === 'listening_text'))
    || (prompt.item.track === 'school' && prompt.item.subject === '英语'
      && prompt.item.answer_type === 'listening_text' && prompt.item.pet_audio_modality === 'listening')
}

/** No TTS, answer transcript, or cloud call is used as the listening stimulus. */
export async function playPetListening(sid: string, prompt: ListeningPromptIdentity, actionId: string,
  isCurrent: () => boolean, signal: AbortSignal, remainingBudget = 4, admitted?: PetMedia,
  verifyOwner?: (media: PetMedia) => Promise<boolean>): Promise<PetListeningResult> {
  activeListening?.cancel()
  const owner = ++playbackOwner
  let playing = false, locallyCancelled = false
  const cancellation = new AbortController()
  const cancel = (): void => { locallyCancelled = true; cancellation.abort(); stop() }
  const wait = <T,>(work: Promise<T>): Promise<T> => new Promise((resolve, reject) => {
    const aborted = (): void => { reject(new Error('PET_LISTENING_CANCELLED')) }
    if (cancellation.signal.aborted) { void work.catch(() => undefined); aborted(); return }
    cancellation.signal.addEventListener('abort', aborted, { once: true })
    work.then(resolve, reject).finally(() => cancellation.signal.removeEventListener('abort', aborted))
  })
  const current = (): boolean => owner === playbackOwner && !locallyCancelled && !signal.aborted && isCurrent()
  const guard = (): void => { if (!current()) throw new Error('PET_LISTENING_CANCELLED') }
  const speechKey = `${actionId}:pet:audio`
  const stop = (): void => { if (owner === playbackOwner && playing) cancelLocalSpeech(speechKey) }
  const validate = (row: PetMedia): PetMedia => {
    if (row.kind !== 'listening' || row.session_id !== sid || row.child_id !== prompt.child_id
      || row.prompt_id !== prompt.prompt_id || row.item_version !== prompt.item_version
      || row.diagnostic_item_id !== prompt.diagnostic_item_id || row.cloud_transmission_allowed !== false
      || row.local_only !== true) throw new Error('PET_LISTENING_BINDING_CHANGED')
    return row
  }
  const settle = async (row: PetMedia): Promise<PetMedia> => {
    // A pending end is already durable. Retry only that original receipt, never audio.
    for (let retry = 0; row.status === 'pending_receipt' && retry < 2; retry++) {
      const request = row.pending_playback_receipt
      if (!request || request.event !== 'ended' || request.media_id !== row.media_id
        || request.generation !== row.generation || request.audio_sha256 !== row.audio.sha256) break
      const delay = row.receipt_retry_after_ms
      if (typeof delay !== 'number' || !Number.isFinite(delay) || delay < 0 || delay > 15_000) break
      await wait(new Promise<void>(resolve => setTimeout(resolve, delay + 25)))
      guard()
      row = validate((await wait(localApi.petMediaPlayback(sid, request))).media)
    }
    return row
  }
  activeListening = { sessionId: sid, mediaId: admitted?.media_id ?? null, owner, cancel }
  signal.addEventListener('abort', cancel, { once: true })
  try {
    guard()
    const collection = await wait(localApi.petPromptMedia(sid, prompt.prompt_id, prompt.item_version))
    guard()
    if (collection.child_id !== prompt.child_id || collection.session_id !== sid
      || collection.prompt_id !== prompt.prompt_id || collection.item_version !== prompt.item_version) throw new Error('PET_LISTENING_BINDING_CHANGED')
    if (collection.listening.length !== 1 || collection.selection_required) return { status: 'unavailable',
      response: collection.listening.length ? '这道题有多份原音，需要先核对对应材料，暂时不计听力成绩。' : '这道题还缺对应原音，我不会用朗读原文代替听力。先保留这题，可以换做其他练习。' }
    let row = validate(collection.listening[0])
    if (activeListening?.owner === owner) activeListening.mediaId = row.media_id
    if (admitted && (validate(admitted).media_id !== row.media_id || admitted.generation !== row.generation
      || admitted.revision !== row.revision || admitted.source_version !== row.source_version || row.status !== 'playing')) {
      throw new Error('PET_LISTENING_ADMISSION_CHANGED')
    }
    for (const phase of ['ended', 'started', 'play']) {
      const key = `${actionId}:pet:${phase}`
      const history = await wait(localApi.petPlaybackActionReceipt(sid, row.media_id, key))
      guard()
      if (history.child_id !== prompt.child_id || history.session_id !== sid || history.media_id !== row.media_id
        || history.client_action_id !== key || history.cloud_transmission_allowed !== false) throw new Error('PET_LISTENING_BINDING_CHANGED')
      row = validate(history.current)
      if (history.replay_decision === 'current_source_invalid') return { status: 'unavailable', media: row,
        response: '原题材料已变化，旧播放记录仍保留，但不会继续播放或用旧结果计算成绩。' }
      if (history.replay_decision === 'reuse_completed') {
        if (phase !== 'ended' || !history.receipt?.completion_accepted) throw new Error('PET_LISTENING_RECEIPT_CHANGED')
        const count = row.qualification.required_play_count
        if (row.status === 'playback_finished' && history.receipt.generation === row.generation
          && count && Number.isInteger(count) && count <= 4
          && (row.qualification.task_complete_play_count ?? 0) < count && remainingBudget > 1) {
          return await playPetListening(sid, prompt, `${actionId}:next`, isCurrent, signal, remainingBudget - 1, undefined, verifyOwner)
        }
        return { status: 'finished', media: row, response: '这次请求的原播放回执已经保存，没有重复播放。可以继续核对原题。' }
      }
      if (history.replay_decision === 'await_recovery') return { status: 'paused', media: row,
        response: '上次播放已经开始，但没有完整播完的确认。记录仍保留，请明确说继续听题再恢复，未自动重播。' }
      if (history.replay_decision === 'retry_pending_receipt') break
      if (history.replay_decision !== 'not_recorded') throw new Error('PET_LISTENING_RECEIPT_CHANGED')
    }
    if (row.status === 'pending_receipt') {
      row = await settle(row)
      return { status: row.status === 'playback_finished' ? 'finished' : 'pending_receipt', media: row,
        response: row.status !== 'playback_finished' ? '上次播放记录仍待核对，先保留进度，不重复播放或计算听力成绩。'
          : row.qualification.required_play_count && (row.qualification.task_complete_play_count ?? 0) < row.qualification.required_play_count
            ? '上次这一遍的播放记录已核对，未重复播放。原题还要求再听，可以继续听题。'
            : '上次原音的播放记录已核对，未重复播放。请继续回答。' }
    }
    if (!row.source_reviewed || ['invalidated', 'cancelled'].includes(row.status)) return {
      status: 'unavailable', media: row, response: '这份原音尚未通过来源核对，或题目已经变化。暂时不播放，也不计听力成绩。' }
    const decodedSource = structuredClone(row)
    const sameOriginal = (candidate: PetMedia, original: PetMedia): boolean => candidate.media_id === original.media_id
      && typeof original.source_version === 'string' && original.source_version.length > 0
      && candidate.source_version === original.source_version && candidate.audio.sha256 === original.audio.sha256
      && candidate.audio.frames === original.audio.frames && candidate.audio.sample_rate === original.audio.sample_rate
      && candidate.audio.status === original.audio.status && candidate.audio.channels === original.audio.channels
      && candidate.audio.sample_width === original.audio.sample_width
    const bytes = await wait(requestPetListeningAudio(sid, row.media_id, row.audio.sha256!, cancellation.signal))
    guard()
    const decoded = await wait(decodePetListening(bytes, row))
    guard()
    if (!admitted) row = validate((await wait(localApi.petMediaControl(sid, { media_id: row.media_id, generation: row.generation,
      expected_revision: row.revision, command: row.status === 'playback_finished' ? 'restart' : 'resume',
      client_action_id: `${actionId}:pet:play` }))).media)
    if (!sameOriginal(row, decodedSource)) throw new Error('PET_LISTENING_SOURCE_CHANGED')
    guard()
    // Freeze the decoded original. A pre-enqueue lookup cannot grant a later playback lease.
    const source = structuredClone(row)
    let pcmControlReference
    try { pcmControlReference = await wait(localApi.petPcmControlReference(sid, row.media_id, row.audio.sha256!, cancellation.signal)) }
    catch { return { status: 'unavailable', media: row, response: '这份原音缺少已核准的完整录音原文，暂时不能安全接收播放中的停止指令。请家长核对完整原文后再听题；原记录保留。' } }
    guard()
    if (!referenceMatchesMedia(pcmControlReference, row)) throw new Error('PET_LISTENING_FULL_REFERENCE_CHANGED')
    let sourceAdmissionRejected = false
    const beforePlayback = async (leaseSignal: AbortSignal): Promise<boolean> => {
      sourceAdmissionRejected = true
      if (leaseSignal.aborted || !current()) return false
      try {
        const fresh = await wait(localApi.petPromptMedia(sid, prompt.prompt_id, prompt.item_version))
        if (leaseSignal.aborted || !current() || fresh.local_only !== true || fresh.cloud_transmission_allowed !== false
          || fresh.session_id !== sid || fresh.child_id !== prompt.child_id || fresh.prompt_id !== prompt.prompt_id
          || fresh.item_version !== prompt.item_version || fresh.selection_required || fresh.listening.length !== 1) return false
        const candidate = validate(fresh.listening[0])
        const latestReference = await wait(localApi.petPcmControlReference(sid, candidate.media_id, candidate.audio.sha256!, leaseSignal))
        const allowed = referenceMatchesMedia(latestReference, candidate) && samePcmReference(pcmControlReference, latestReference)
          && sameOriginal(candidate, source)
          && candidate.generation === source.generation && candidate.revision === source.revision
          && candidate.status === 'playing' && candidate.source_reviewed === true
          && (!verifyOwner || await verifyOwner(candidate))
          && !leaseSignal.aborted && current()
        sourceAdmissionRejected = !allowed
        return allowed
      } catch { return false }
    }
    let start: Promise<PetMedia> | null = null
    let startedAt = 0
    let startFailure: unknown = null
    playing = true
    const disposition = await wait(queueLocalPcm(decoded.pcm, decoded.sampleRate, sid, () => {
      startedAt = Math.floor(performance.now())
      const receipt: PetPlaybackReceipt = { media_id: row.media_id, generation: row.generation,
        expected_revision: row.revision, client_action_id: `${actionId}:pet:started`,
        event: 'started', audio_sha256: row.audio.sha256!, frame_position: 0, monotonic_ms: startedAt }
      start = localApi.petMediaPlayback(sid, receipt).then(value => validate(value.media))
      void start.catch(error => { startFailure = error; if (current()) stop() })
    }, { speechKey, isCurrent: current, localOnly: true, beforePlayback, pcmControlReference }))
    const endedAt = Math.floor(performance.now())
    playing = false
    if (start) row = await wait(start)
    if (startFailure) throw startFailure
    if (!start && sourceAdmissionRejected) return { status: 'unavailable', media: row,
      response: '播放前无法确认这份原题音频仍有效，暂时不播放，也不计听力成绩。原记录保留，核对材料后可以继续。' }
    if (disposition !== 'finished' || !current() || !start) {
      if (start && current()) row = validate((await localApi.petMediaControl(sid, { media_id: row.media_id,
        generation: row.generation, expected_revision: row.revision, command: 'pause', client_action_id: `${actionId}:pet:pause` })).media)
      return { status: 'paused', media: row, response: '原音没有完整播完，未记作已听完。进度已保留，可以继续听题。' }
    }
    if (verifyOwner && !await wait(verifyOwner(row))) return { status: 'unavailable', media: row,
      response: '当前考试或原音状态已变化，旧回调未记作完整听完。原记录仍保留，请核对当前题目后再继续。' }
    guard()
    const ended: PetPlaybackReceipt = { media_id: row.media_id, generation: row.generation,
      expected_revision: row.revision, client_action_id: `${actionId}:pet:ended`, event: 'ended',
      audio_sha256: row.audio.sha256!, frame_position: row.audio.frames!, monotonic_ms: endedAt }
    row = await settle(validate((await wait(localApi.petMediaPlayback(sid, ended))).media))
    guard()
    const required = row.qualification.required_play_count
    if (row.status === 'playback_finished' && required && Number.isInteger(required) && required <= 4
      && (row.qualification.task_complete_play_count ?? 0) < required && remainingBudget > 1) {
      return await playPetListening(sid, prompt, `${actionId}:next`, isCurrent, signal, remainingBudget - 1, undefined, verifyOwner)
    }
    return { status: row.status === 'playback_finished' ? 'finished' : 'pending_receipt', media: row,
      response: row.status !== 'playback_finished' ? '原音已播放，播放记录还在核对，暂时不计成绩。'
        : row.qualification.required_play_count && (row.qualification.task_complete_play_count ?? 0) < row.qualification.required_play_count
          ? '这一遍播完了，原题还要求再听。准备好后说继续听题。' : '原音播完了，请说出你的答案。' }
  } catch (error) {
    if (locallyCancelled || signal.aborted) return { status: 'paused', response: '原音已暂停，未记作完整听完。' }
    throw error
  } finally {
    stop()
    if (activeListening?.owner === owner) activeListening = null
    signal.removeEventListener('abort', cancel)
  }
}
