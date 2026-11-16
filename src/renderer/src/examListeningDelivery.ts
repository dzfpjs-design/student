import { localApi } from './api'
import { isPetListening, playPetListening, stopPetListening, type ListeningPromptIdentity, type PetListeningResult } from './petListeningPlayback'
import type { PetMedia } from './petMedia'
import type { DiagnosticItem } from './types'
import type { VoiceQuestionReference } from './voiceCaptureContext'
import { SupersededDeviceCommand } from './deviceCommandReceipt'

export type ExamAnswerBinding = Extract<VoiceQuestionReference, { kind: 'exam' }>['binding']
export type ExamListeningDelivery = {
  version: 'exam_listening_delivery_v1'; child_id: string; session_id: string; run_id: string
  position: number; item_id: string; unit_key: string; prompt_id: string; item_version: string
  media_item_id: string; control_revision: number; playback_allowed: boolean
  resource: null | { media_id: string; source_version: string; audio_sha256: string; item_version: string
    requirement_version: string; required_play_count: number; generation: number; revision: number
    status: string; completed_play_count: number }
  media_prompt: ListeningPromptIdentity & { origin_session_id: string; owner_kind: 'exam'; item: DiagnosticItem }
  local_only: true; cloud_transmission_allowed: false
}
export type ExamListeningState = {
  run_id: string; child_id: string; session_id: string; status: string; current_position: number
  delivery_status: string; started_at?: string | null; elapsed_seconds?: number
  current_item: (DiagnosticItem & { exam_audio_modality?: 'listening' }) | null
  audio_delivery?: ExamListeningDelivery | null; answer_binding?: ExamAnswerBinding | null
}
export type ExamListeningView = { runId: string; position: number; item: DiagnosticItem | null; status: string; message: string }
type Execution = { capability: string; result: unknown; replayed?: boolean; recorded_receipt?: boolean; replay_audio_allowed?: boolean }
type Context = { sessionId: string; childId: string; turnId: string; execution: Execution; isCurrent: () => boolean
  signal: AbortSignal; currentQuestion?: VoiceQuestionReference | null; currentViewRunId?: string | null
  capture: (binding: ExamAnswerBinding | null) => void; present: (view: ExamListeningView) => void }
const activeExamAudio = new Map<string, { childId: string; runId: string; mediaId: string; owner: symbol }>()
const consumedExecutions = new Set<string>()
const object = (value: unknown): Record<string, unknown> | null => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
const text = (value: unknown): value is string => typeof value === 'string' && value.trim().length > 0
const integer = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) >= 0
const bindingSame = (a: ExamAnswerBinding | null | undefined, b: ExamAnswerBinding | null | undefined): boolean => Boolean(a && b
  && a.run_id === b.run_id && a.position === b.position && a.item_id === b.item_id && a.source_version === b.source_version)
function resultState(execution: Execution): Record<string, unknown> | null {
  const result = object(execution.result)
  return execution.capability === 'exam.submit' ? object(result?.exam) : result
}
function checkedCue(state: ExamListeningState, cid: string, sid: string): ExamListeningDelivery {
  const cue = state.audio_delivery, p = cue?.media_prompt
  if (state.child_id !== cid || state.session_id !== sid || !cue || cue.version !== 'exam_listening_delivery_v1' || cue.local_only !== true || cue.cloud_transmission_allowed !== false
    || cue.child_id !== cid || cue.session_id !== sid || cue.run_id !== state.run_id || cue.position !== state.current_position
    || !integer(cue.position) || cue.position < 1 || !integer(cue.control_revision) || !text(cue.unit_key)
    || !text(cue.prompt_id) || !text(cue.item_version) || !text(cue.media_item_id)
    || (state.current_item ? cue.item_id !== state.current_item.diagnostic_item_id
      || state.current_item.stimulus_text !== null || state.current_item.stimulus_asset !== null
      : !['completed', 'cancelled', 'timed_out'].includes(state.status)) || p?.owner_kind !== 'exam'
    || p.child_id !== cid || p.origin_session_id !== sid || p.prompt_id !== cue.prompt_id
    || p.item_version !== cue.item_version || p.diagnostic_item_id !== cue.media_item_id
    || p.item.diagnostic_item_id !== cue.media_item_id || p.item.stimulus_text !== null || p.item.stimulus_asset !== null
    || p.item.pet_audio_modality !== 'listening' || !isPetListening(p)) throw new Error('EXAM_LISTENING_OWNER_CHANGED')
  if (state.status === 'active' && (!state.answer_binding || state.answer_binding.run_id !== cue.run_id
    || state.answer_binding.position !== cue.position || state.answer_binding.item_id !== cue.item_id
    || !text(state.answer_binding.source_version))) throw new Error('EXAM_LISTENING_ANSWER_OWNER_CHANGED')
  return cue
}
function sameCue(a: ExamListeningDelivery, b: ExamListeningDelivery): boolean {
  return a.version === b.version && a.child_id === b.child_id && a.session_id === b.session_id && a.run_id === b.run_id
    && a.position === b.position && a.item_id === b.item_id && a.unit_key === b.unit_key && a.prompt_id === b.prompt_id
    && a.item_version === b.item_version && a.media_item_id === b.media_item_id && a.control_revision === b.control_revision
    && a.resource?.media_id === b.resource?.media_id && a.resource?.source_version === b.resource?.source_version
    && a.resource?.audio_sha256 === b.resource?.audio_sha256 && a.resource?.requirement_version === b.resource?.requirement_version
    && a.resource?.required_play_count === b.resource?.required_play_count
}
function checkedMedia(media: PetMedia, cue: ExamListeningDelivery, exactRevision = false): PetMedia {
  const r = cue.resource, q = media.qualification
  if (!r || !text(r.source_version) || !/^[a-f0-9]{64}$/i.test(r.audio_sha256) || !text(r.requirement_version)
    || !integer(r.required_play_count) || r.required_play_count < 1 || r.required_play_count > 3
    || !integer(r.generation) || !integer(r.revision) || !integer(r.completed_play_count)
    || r.item_version !== cue.item_version || media.kind !== 'listening' || media.local_only !== true
    || media.cloud_transmission_allowed !== false || media.source_reviewed !== true
    || ['invalidated', 'cancelled'].includes(media.status) || media.child_id !== cue.child_id || media.session_id !== cue.session_id
    || media.prompt_id !== cue.prompt_id || media.item_version !== cue.item_version || media.diagnostic_item_id !== cue.media_item_id
    || media.media_id !== r.media_id || media.source_version !== r.source_version || media.audio.sha256 !== r.audio_sha256
    || q.delivery_requirement?.version !== r.requirement_version || q.required_play_count !== r.required_play_count
    || (exactRevision && (media.generation !== r.generation || media.revision !== r.revision || media.status !== r.status
      || q.task_complete_play_count !== r.completed_play_count))) throw new Error('EXAM_LISTENING_MEDIA_CHANGED')
  return media
}
function stopOwnedExam(context: Context, runId: string): void {
  const active = activeExamAudio.get(context.sessionId)
  if (active?.childId === context.childId && active.runId === runId) stopPetListening(context.sessionId, active.mediaId)
}
function clearOwnedCapture(context: Context, runId: string): void {
  if (context.currentQuestion?.kind === 'exam' && context.currentQuestion.binding.run_id === runId) context.capture(null)
}

/** The canonical audio carrier never becomes a diagnostic answer owner. */
export async function consumeExamListeningExecution(context: Context): Promise<{ handled: boolean; playback?: PetListeningResult; response?: string }> {
  const { execution, sessionId: sid, childId: cid, signal } = context
  if (!['exam.start', 'exam.resume', 'exam.submit', 'exam.pause', 'exam.cancel'].includes(execution.capability)) return { handled: false }
  const current = (): boolean => !signal.aborted && context.isCurrent()
  const guard = (): void => { if (!current()) throw new SupersededDeviceCommand() }
  guard()
  const result = resultState(execution)
  const proposed = result as unknown as ExamListeningState | null
  const replay = execution.replayed === true || execution.recorded_receipt === true || execution.replay_audio_allowed === false
  // Ordinary exams use the same scoped server status. A missing run identity
  // or absent binding cannot erase or replace another pending original answer.
  if (!text(proposed?.run_id)) return { handled: true }
  if (proposed.child_id !== cid || proposed.session_id !== sid) throw new Error('EXAM_LISTENING_OWNER_CHANGED')
  const expected = proposed.audio_delivery ? checkedCue(proposed, cid, sid) : null
  const read = async (): Promise<ExamListeningState | null> => {
    guard()
    const state = (await localApi.activeExamSimulation(sid)).exam
    guard()
    if (state && (state.child_id !== cid || state.session_id !== sid)) throw new Error('EXAM_LISTENING_OWNER_CHANGED')
    return state
  }
  const state = await read()
  // A historical A receipt must not stop, clear, or render over current run B.
  if (state && state.run_id !== proposed.run_id) return { handled: true,
    response: '原考试回执已核对，当前考试已变化。没有操作当前考试或重复播放原音。' }
  if (!state) {
    if (!['completed', 'cancelled', 'timed_out'].includes(proposed.status)) throw new Error('EXAM_LISTENING_OWNER_CHANGED')
    if ((context.currentQuestion?.kind === 'exam' && context.currentQuestion.binding.run_id !== proposed.run_id)
      || (context.currentViewRunId && context.currentViewRunId !== proposed.run_id)) return { handled: true }
    stopOwnedExam(context, proposed.run_id)
    clearOwnedCapture(context, proposed.run_id)
    const response = proposed.status === 'cancelled' ? '原考试已取消，原音已停止。' : '原考试已结束，作答记录已经保留。'
    context.present({ runId: proposed.run_id, position: proposed.current_position, item: null, status: proposed.status, message: response })
    return { handled: true, response }
  }
  if (!state.audio_delivery) {
    const project = (item: DiagnosticItem | null, message: string): void => {
      guard(); context.present({ runId: state.run_id, position: state.current_position, item, status: state.status, message })
    }
    if (state.status !== 'active') {
      stopOwnedExam(context, state.run_id)
      clearOwnedCapture(context, state.run_id)
      const response = state.status === 'paused' ? '考试已暂停。准备好后说“继续模拟卷”，再核对当前原题。' : '原考试已结束，作答记录已经保留。'
      project(null, response)
      return { handled: true, response }
    }
    const item = state.current_item, binding = state.answer_binding
    const listening = item?.exam_audio_modality === 'listening' || item?.pet_audio_modality === 'listening' || item?.answer_type === 'listening_text'
    const valid = item && text(item.prompt) && text(item.diagnostic_item_id) && integer(state.current_position) && state.current_position >= 1
      && binding && binding.run_id === state.run_id && binding.position === state.current_position
      && binding.item_id === item.diagnostic_item_id && text(binding.source_version)
      && (replay || (bindingSame(proposed.answer_binding, binding) && proposed.current_position === state.current_position
        && proposed.current_item?.diagnostic_item_id === item.diagnostic_item_id))
    if (!valid || listening) {
      const response = listening ? '当前听力原材料尚未核对，暂时不能开始播放或作答。'
        : '当前原题来源尚未核对一致，暂时没有可确认的原题。请先核对当前考试进度。'
      project(null, response)
      return { handled: true, response }
    }
    stopOwnedExam(context, state.run_id)
    context.capture(binding)
    const response = replay ? '当前考试原题已恢复，请独立作答。' : '已切换到当前考试原题，请独立作答。'
    project(item, response)
    return { handled: true, response }
  }
  const cue = checkedCue(state, cid, sid)
  const view = (message: string): void => { guard(); context.present({ runId: state.run_id, position: state.current_position,
    item: state.current_item, status: state.status, message }) }
  if (state.status !== 'active' || !cue.playback_allowed || ['preparing', 'ready'].includes(state.delivery_status)) {
    stopOwnedExam(context, state.run_id)
    clearOwnedCapture(context, state.run_id)
    const response = state.delivery_status === 'preparing' ? '原音还在核对，考试尚未开始计时。'
      : state.delivery_status === 'ready' ? '原音材料已齐备。准备好后说“继续模拟卷”，再开始计时。' : '考试已暂停，原音没有继续播放。'
    view(response)
    return { handled: true, response }
  }
  if (replay) {
    context.capture(state.answer_binding!)
    const response = '原考试进度已恢复。没有自动重播原音，需要继续听时请明确告诉我。'
    view(response)
    return { handled: true, response }
  }
  const key = `${cid}:${sid}:${context.turnId}`
  if (consumedExecutions.has(key)) return { handled: true, response: '原考试回执已处理，没有重复播放原音。' }
  if (!expected || !sameCue(expected, cue) || !bindingSame(proposed.answer_binding, state.answer_binding)
    || expected.resource?.generation !== cue.resource?.generation || expected.resource?.revision !== cue.resource?.revision
    || expected.resource?.status !== cue.resource?.status || expected.resource?.completed_play_count !== cue.resource?.completed_play_count) throw new Error('EXAM_LISTENING_CURRENT_SLOT_CHANGED')
  consumedExecutions.add(key)
  context.capture(state.answer_binding!)
  const mediaSet = await localApi.petPromptMedia(sid, cue.prompt_id, cue.item_version)
  guard()
  if (mediaSet.child_id !== cid || mediaSet.session_id !== sid || mediaSet.prompt_id !== cue.prompt_id
    || mediaSet.item_version !== cue.item_version || mediaSet.local_only !== true || mediaSet.cloud_transmission_allowed !== false
    || mediaSet.selection_required || mediaSet.listening.length !== 1) throw new Error('EXAM_LISTENING_MEDIA_CHANGED')
  let media = checkedMedia(mediaSet.listening[0], cue, true)
  if ((media.qualification.task_complete_play_count ?? 0) >= cue.resource!.required_play_count
      && media.qualification.task_delivery_qualified === true) {
    view('本组原音已按要求听完，已保存的真实播放记录继续用于这一题。请回答当前题目。')
    return { handled: true, response: '本组原音已听足，请回答当前题目。' }
  }
  if ((media.qualification.task_complete_play_count ?? 0) >= cue.resource!.required_play_count) {
    const response = '原播放记录已经保存，当前听力资格仍待核对。没有自动重播，也不计听力成绩。'
    view(response)
    return { handled: true, response }
  }
  if (media.status === 'pending_receipt') {
    const response = '原音播放记录还在核对，暂时不重复播放，也不计听力成绩。'
    view(response)
    return { handled: true, response }
  }
  const latest = await read(), latestCue = latest ? checkedCue(latest, cid, sid) : null
  if (!latest || latest.run_id !== cue.run_id || latest.status !== 'active' || !latestCue?.playback_allowed || (!latestCue || !sameCue(cue, latestCue))
    || !bindingSame(state.answer_binding, latest.answer_binding)) throw new Error('EXAM_LISTENING_CURRENT_SLOT_CHANGED')
  media = checkedMedia((await localApi.petMediaControl(sid, { media_id: media.media_id, generation: media.generation,
    expected_revision: media.revision, command: 'resume', client_action_id: `${context.turnId}:exam:admit` })).media, cue)
  guard()
  if (media.status !== 'playing') throw new Error('EXAM_LISTENING_ADMISSION_CHANGED')
  const verifyOwner = async (candidate: PetMedia): Promise<boolean> => {
    if (!current()) return false
    try {
      const live = await read()
      if (!live || live.run_id !== cue.run_id) return false
      const liveCue = checkedCue(live, cid, sid)
      checkedMedia(candidate, liveCue)
      return current() && live.status === 'active' && liveCue.playback_allowed && sameCue(cue, liveCue)
        && bindingSame(state.answer_binding, live.answer_binding) && liveCue.resource?.generation === candidate.generation
    } catch { return false }
  }
  const owner = Symbol('exam-audio-command')
  activeExamAudio.set(sid, { childId: cid, runId: cue.run_id, mediaId: media.media_id, owner })
  view('正在播放这组原听力材料，播完后再回答当前题目。')
  try {
    const played = await playPetListening(sid, cue.media_prompt, `${context.turnId}:exam`, current, signal, 4, media, verifyOwner)
    guard()
    const after = await read()
    const afterCue = after?.audio_delivery ? checkedCue(after, cid, sid) : null
    if (after?.status === 'active' && after.run_id === cue.run_id && afterCue && sameCue(cue, afterCue)
      && bindingSame(state.answer_binding, after.answer_binding)) {
      view(played.response)
      return { handled: true, playback: played, response: played.response }
    }
    return { handled: true, playback: played, response: '原音已停止，考试进度已变化。请先核对当前考试。' }
  } finally {
    if (activeExamAudio.get(sid)?.owner === owner) activeExamAudio.delete(sid)
  }
}
