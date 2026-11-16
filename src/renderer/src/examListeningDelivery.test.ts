import { beforeEach, expect, it, vi } from 'vitest'
import { localApi, requestPetListeningAudio } from './api'
import { cancelLocalSpeech, queueLocalPcm } from './LocalInputs'
import { decodePetListening } from './petListeningAudio'
import { consumeExamListeningExecution, type ExamListeningState } from './examListeningDelivery'
import type { PetMedia } from './petMedia'
import type { DiagnosticItem } from './types'
import type { VoiceQuestionReference } from './voiceCaptureContext'

vi.mock('./api', () => ({ localApi: { activeExamSimulation: vi.fn(), petPromptMedia: vi.fn(), petPlaybackActionReceipt: vi.fn(), petMediaControl: vi.fn(), petMediaPlayback: vi.fn(), petPcmControlReference: vi.fn() }, requestPetListeningAudio: vi.fn() }))
vi.mock('./LocalInputs', () => ({ queueLocalPcm: vi.fn(), cancelLocalSpeech: vi.fn() }))
vi.mock('./petListeningAudio', () => ({ decodePetListening: vi.fn() }))
let row: PetMedia, state: ExamListeningState, sequence = 0
const capture = vi.fn(), present = vi.fn()
const item = (id: string): DiagnosticItem => ({ diagnostic_item_id: id, knowledge_node_id: 'school-listening-node',
  track: 'school', subject: '英语', title: '听力选择', prompt: id === 'canonical-item' ? 'Where will they meet?' : 'What time will they meet?',
  answer_type: 'listening_text', difficulty: 2, assessment_mode: 'objective', pet_audio_modality: 'listening', stimulus_text: null, stimulus_asset: null })
function projection(position = state?.current_position ?? 1): ExamListeningState {
  const slot = position === 1 ? 'canonical-item' : 'second-item'
  return { run_id: 'exam-run', child_id: 'child', session_id: 'session', status: state?.status ?? 'active',
    current_position: position, delivery_status: state?.delivery_status ?? 'active', current_item: item(slot),
    started_at: state?.started_at ?? new Date().toISOString(), elapsed_seconds: state?.elapsed_seconds ?? 1,
    answer_binding: state?.status === 'paused' ? null : { run_id: 'exam-run', position, item_id: slot, source_version: `slot-version-${position}` },
    audio_delivery: { version: 'exam_listening_delivery_v1', run_id: 'exam-run', position, item_id: slot,
      child_id: 'child', session_id: 'session', unit_key: 'audited-shared-unit', prompt_id: 'canonical-prompt', item_version: 'canonical-version',
      media_item_id: 'canonical-item', control_revision: state?.audio_delivery?.control_revision ?? 1,
      playback_allowed: (state?.status ?? 'active') === 'active',
      resource: { media_id: row.media_id, source_version: row.source_version, audio_sha256: row.audio.sha256!, item_version: row.item_version,
        requirement_version: 'requirement-v1', required_play_count: row.qualification.required_play_count!,
        completed_play_count: row.qualification.task_complete_play_count!, generation: row.generation, revision: row.revision, status: row.status },
      media_prompt: { prompt_id: 'canonical-prompt', item_version: 'canonical-version', child_id: 'child', origin_session_id: 'session',
        diagnostic_item_id: 'canonical-item', owner_kind: 'exam', item: item('canonical-item') },
      local_only: true, cloud_transmission_allowed: false } }
}
const envelope = () => ({ media: structuredClone(row), local_only: true as const, cloud_transmission_allowed: false as const })
beforeEach(() => {
  vi.resetAllMocks(); sequence++
  row = { media_id: 'media', child_id: 'child', session_id: 'session', prompt_id: 'canonical-prompt', item_version: 'canonical-version',
    diagnostic_item_id: 'canonical-item', kind: 'listening', source_version: 'audio-source-v1', generation: 0, revision: 0,
    status: 'ready', source_reviewed: true, audio: { status: 'valid', sha256: 'a'.repeat(64), frames: 16000, sample_rate: 16000, channels: 1, sample_width: 2 },
    qualification: { required_play_count: 2, task_complete_play_count: 0, task_delivery_qualified: false, delivery_requirement: { version: 'requirement-v1' } },
    local_only: true, cloud_transmission_allowed: false } as PetMedia
  state = undefined as unknown as ExamListeningState; state = projection(1)
  vi.mocked(localApi.activeExamSimulation).mockImplementation(async () => ({ exam: { ...structuredClone(state), audio_delivery: state.audio_delivery ? {
    ...structuredClone(state.audio_delivery), resource: state.audio_delivery.resource ? projection().audio_delivery!.resource : null } : null } }))
  vi.mocked(localApi.petPromptMedia).mockImplementation(async () => ({ child_id: 'child', session_id: 'session', prompt_id: 'canonical-prompt',
    item_version: 'canonical-version', listening: [structuredClone(row)], speaking: [], selection_required: false, status: 'bound_media', local_only: true, cloud_transmission_allowed: false }))
  vi.mocked(localApi.petPcmControlReference).mockImplementation(async (sid, mid, hash) => {
    expect([sid, mid, hash]).toEqual([row.session_id, row.media_id, row.audio.sha256])
    expect(row.status).toBe('playing'); expect(row.generation).toBeGreaterThanOrEqual(1)
    return { version: 'pet_full_audio_control_reference_v1', child_id: row.child_id, session_id: row.session_id,
      media_id: row.media_id, prompt_id: row.prompt_id, item_version: row.item_version,
      diagnostic_item_id: row.diagnostic_item_id, source_version: row.source_version, generation: row.generation,
      audio_sha256: row.audio.sha256!, spoken_text_sha256: 'e'.repeat(64), source_review_event_id: 'parent-full-original-review',
      local_only: true, cloud_transmission_allowed: false }
  })
  vi.mocked(localApi.petPlaybackActionReceipt).mockImplementation(async (_, __, key) => ({ child_id: 'child', session_id: 'session', media_id: 'media', client_action_id: key,
    replay_decision: 'not_recorded', receipt: null, current: structuredClone(row), automatic_playback_allowed: false, local_only: true, cloud_transmission_allowed: false }))
  vi.mocked(localApi.petMediaControl).mockImplementation(async (_, request) => { row.generation++;row.revision++;row.status = request.command === 'pause' ? 'paused' : 'playing';return envelope() })
  vi.mocked(localApi.petMediaPlayback).mockImplementation(async (_, receipt) => {
    expect(receipt.generation).toBe(row.generation);expect(receipt.expected_revision).toBe(row.revision)
    row.revision++;if(receipt.event === 'ended'){row.status='playback_finished';row.qualification.task_complete_play_count!++;row.qualification.task_delivery_qualified=row.qualification.task_complete_play_count!>=row.qualification.required_play_count!}return envelope()
  })
  vi.mocked(requestPetListeningAudio).mockResolvedValue(new ArrayBuffer(44))
  vi.mocked(decodePetListening).mockResolvedValue({ pcm: new ArrayBuffer(32000), sampleRate: 16000 })
  vi.mocked(queueLocalPcm).mockImplementation(async (_, __, ___, start, options) => {
    if(!await options!.beforePlayback!(new AbortController().signal))return 'superseded'
    start?.();return 'finished'
  })
})
function run(overrides: Partial<Parameters<typeof consumeExamListeningExecution>[0]> = {}, execution: Partial<Parameters<typeof consumeExamListeningExecution>[0]['execution']> = {}) {
  return consumeExamListeningExecution({ sessionId:'session',childId:'child',turnId:`turn-${sequence}`,signal:new AbortController().signal,
    isCurrent:()=>true,capture,present,currentQuestion:null,execution:{capability:'exam.start',result:structuredClone(state),...execution},...overrides })
}

it('consumes fresh exam execution through real media control, PCM callback and two formal ended receipts', async () => {
  const played=await run()
  expect(played.playback?.status).toBe('finished')
  expect(queueLocalPcm).toHaveBeenCalledTimes(2)
  expect(vi.mocked(queueLocalPcm).mock.calls.map(call => call[4]!.pcmControlReference)).toEqual([1, 2].map(generation => ({
    version: 'pet_full_audio_control_reference_v1', child_id: 'child', session_id: 'session', media_id: 'media',
    prompt_id: 'canonical-prompt', item_version: 'canonical-version', diagnostic_item_id: 'canonical-item',
    source_version: 'audio-source-v1', generation, audio_sha256: 'a'.repeat(64), spoken_text_sha256: 'e'.repeat(64),
    source_review_event_id: 'parent-full-original-review', local_only: true, cloud_transmission_allowed: false,
  })))
  expect(vi.mocked(localApi.petMediaPlayback).mock.calls.map(x=>x[1].event)).toEqual(['started','ended','started','ended'])
  expect(capture).toHaveBeenCalledWith(state.answer_binding)
  expect(vi.mocked(capture).mock.calls.some(x=>(x[0] as VoiceQuestionReference)?.kind==='diagnostic')).toBe(false)
  expect(vi.mocked(localApi.petPromptMedia).mock.calls.every(x=>x[1]==='canonical-prompt'&&x[2]==='canonical-version')).toBe(true)
})
it.each(['child','session','run','position','item','version','canonical','owner','transcript'] as const)('rejects crossed %s cue before audio or control', async change=>{
  const wrong=structuredClone(state),cue=wrong.audio_delivery!
  if(change==='child')cue.child_id='other'
  if(change==='session')cue.session_id='other'
  if(change==='run')cue.run_id='other'
  if(change==='position')cue.position=2
  if(change==='item')cue.item_id='other'
  if(change==='version')cue.version='unknown' as never
  if(change==='canonical')cue.media_prompt.item_version='other'
  if(change==='owner')cue.media_prompt.owner_kind='diagnostic' as never
  if(change==='transcript')cue.media_prompt.item.stimulus_text='Full original transcript'
  await expect(run({}, {result:wrong})).rejects.toThrow('EXAM_LISTENING_')
  expect(queueLocalPcm).not.toHaveBeenCalled();expect(localApi.petMediaControl).not.toHaveBeenCalled()
})
it.each(['source','hash','requirement','generation','revision'] as const)('rejects changed canonical media %s from GET', async change=>{
  const original=structuredClone(state)
  if(change==='source')row.source_version='withdrawn-source'
  if(change==='hash')row.audio.sha256='b'.repeat(64)
  if(change==='requirement')row.qualification.delivery_requirement!.version='other'
  if(change==='generation')row.generation++
  if(change==='revision')row.revision++
  await expect(run({}, {result:original})).rejects.toThrow('EXAM_LISTENING_')
  expect(queueLocalPcm).not.toHaveBeenCalled();expect(localApi.petMediaControl).not.toHaveBeenCalled()
})
it('uses shared canonical audio for the second different item without adopting its prompt version as media identity or replaying', async ()=>{
  await run();vi.mocked(queueLocalPcm).mockClear();vi.mocked(localApi.petMediaControl).mockClear();vi.mocked(localApi.petMediaPlayback).mockClear()
  state=projection(2)
  const played=await run({turnId:`turn-${sequence}-next`},{capability:'exam.submit',result:{exam:structuredClone(state)}})
  expect(played.handled).toBe(true);expect(played.response).toContain('已听足')
  expect(capture).toHaveBeenLastCalledWith({run_id:'exam-run',position:2,item_id:'second-item',source_version:'slot-version-2'})
  expect(localApi.petPromptMedia).toHaveBeenLastCalledWith('session','canonical-prompt','canonical-version')
  expect(queueLocalPcm).not.toHaveBeenCalled();expect(localApi.petMediaControl).not.toHaveBeenCalled();expect(localApi.petMediaPlayback).not.toHaveBeenCalled()
})
it.each([{replayed:true},{recorded_receipt:true},{replay_audio_allowed:false}])('projects cached execution %j without media consumption', async replay=>{
  await run({},replay);expect(localApi.petPromptMedia).not.toHaveBeenCalled();expect(queueLocalPcm).not.toHaveBeenCalled();expect(localApi.petMediaControl).not.toHaveBeenCalled()
})
it('does not consume the same fresh execution twice', async()=>{await run();vi.mocked(queueLocalPcm).mockClear();vi.mocked(localApi.petMediaControl).mockClear();await run();expect(queueLocalPcm).not.toHaveBeenCalled();expect(localApi.petMediaControl).not.toHaveBeenCalled()})
it.each(['preparing','ready'])('keeps ordinary unsubmitted owner and stopped clock during %s preparation', async phase=>{
  state.status='paused';state.delivery_status=phase;state.started_at=null;state.elapsed_seconds=0;state.answer_binding=null;state.audio_delivery!.playback_allowed=false;state.audio_delivery!.resource=null
  const result=await run({currentQuestion:{kind:'diagnostic',binding:{prompt_id:'ordinary',item_version:'ordinary-v'}}})
  if(phase==='ready')expect(result.response).toContain('继续模拟卷')
  expect(capture).not.toHaveBeenCalled();expect(present).toHaveBeenCalledWith(expect.objectContaining({status:'paused'}));expect(localApi.petMediaControl).not.toHaveBeenCalled();expect(queueLocalPcm).not.toHaveBeenCalled();expect(state.elapsed_seconds).toBe(0)
})
it('does not clear an ordinary question when an exam start returns no binding', async()=>{
  await run({currentQuestion:{kind:'diagnostic',binding:{prompt_id:'ordinary',item_version:'v'}}},{result:{status:'awaiting_resources',answer_binding:{run_id:'old-run',position:1,item_id:'old',source_version:'old-v'}}})
  expect(capture).not.toHaveBeenCalled();expect(localApi.activeExamSimulation).not.toHaveBeenCalled()
})
it('denies a stale queued lease after exam control revision changes before PCM starts', async()=>{
  vi.mocked(queueLocalPcm).mockImplementation(async(_,__,___,start,options)=>{state.audio_delivery!.control_revision++;const allowed=await options!.beforePlayback!(new AbortController().signal);expect(allowed).toBe(false);if(allowed)start?.();return 'superseded'})
  expect((await run()).playback?.status).toBe('unavailable');expect(localApi.petMediaPlayback).not.toHaveBeenCalled()
})
it('does not fabricate ended from an old callback after pause or unit withdrawal', async()=>{
  row.qualification.required_play_count=1;state=projection(1)
  let finish!:(value:'finished')=>void
  vi.mocked(queueLocalPcm).mockImplementation(async(_,__,___,start,options)=>{expect(await options!.beforePlayback!(new AbortController().signal)).toBe(true);start?.();return new Promise(resolve=>{finish=resolve})})
  const playing=run();await vi.waitFor(()=>expect(finish).toBeTypeOf('function'))
  state.status='paused';state.audio_delivery!.control_revision++;state.audio_delivery!.playback_allowed=false
  finish('finished');expect((await playing).playback?.status).toBe('unavailable');expect(vi.mocked(localApi.petMediaPlayback).mock.calls.map(x=>x[1].event)).toEqual(['started'])
})
it('cancels interrupted sound without an ended receipt and does not accept a late completion', async()=>{
  const abort=new AbortController();let finish!:(value:'finished')=>void
  vi.mocked(queueLocalPcm).mockImplementation(async(_,__,___,start,options)=>{expect(await options!.beforePlayback!(new AbortController().signal)).toBe(true);start?.();return new Promise(resolve=>{finish=resolve})})
  const playing=run({signal:abort.signal});await vi.waitFor(()=>expect(finish).toBeTypeOf('function'));abort.abort()
  await expect(playing).rejects.toThrow();expect(cancelLocalSpeech).toHaveBeenCalled();finish('finished');await Promise.resolve()
  expect(vi.mocked(localApi.petMediaPlayback).mock.calls.map(x=>x[1].event)).toEqual(['started'])
})
it('rejects late source reads after privacy or child navigation before media control', async()=>{
  let current=true
  vi.mocked(localApi.petPromptMedia).mockImplementation(async()=>{current=false;return {child_id:'child',session_id:'session',prompt_id:'canonical-prompt',item_version:'canonical-version',listening:[row],speaking:[],selection_required:false,status:'bound_media',local_only:true,cloud_transmission_allowed:false}})
  await expect(run({isCurrent:()=>current})).rejects.toThrow();expect(localApi.petMediaControl).not.toHaveBeenCalled();expect(queueLocalPcm).not.toHaveBeenCalled()
})

it.each(['exam.pause','exam.cancel'])('a late cached %s for run A cannot stop PCM or clear the answer owner of current run B', async capability=>{
  row.qualification.required_play_count=1;state=projection(1)
  const old=structuredClone(state);old.run_id='run-A';old.audio_delivery!.run_id='run-A';old.answer_binding=null
  old.status=capability==='exam.cancel'?'cancelled':'paused';old.audio_delivery!.playback_allowed=false
  if(capability==='exam.cancel')old.current_item=null
  state.run_id='run-B';state.audio_delivery!.run_id='run-B';state.answer_binding!.run_id='run-B'
  let finish!:(value:'finished')=>void
  vi.mocked(queueLocalPcm).mockImplementation(async(_,__,___,start,options)=>{expect(await options!.beforePlayback!(new AbortController().signal)).toBe(true);start?.();return new Promise(resolve=>{finish=resolve})})
  const playing=run();await vi.waitFor(()=>expect(finish).toBeTypeOf('function'))
  capture.mockClear();present.mockClear();vi.mocked(cancelLocalSpeech).mockClear()
  await run({turnId:`old-${sequence}`,currentQuestion:{kind:'exam',binding:state.answer_binding!}}, {capability,result:old,recorded_receipt:true,replay_audio_allowed:false})
  expect(capture).not.toHaveBeenCalled();expect(present).not.toHaveBeenCalled();expect(cancelLocalSpeech).not.toHaveBeenCalled()
  finish('finished');expect((await playing).playback?.status).toBe('finished')
})
it('a confirmed cancellation stops only its own real PCM and never accepts the late ended callback', async()=>{
  row.qualification.required_play_count=1;state=projection(1)
  let finish!:(value:'finished')=>void
  vi.mocked(queueLocalPcm).mockImplementation(async(_,__,___,start,options)=>{expect(await options!.beforePlayback!(new AbortController().signal)).toBe(true);start?.();return new Promise(resolve=>{finish=resolve})})
  const owner=structuredClone(state.answer_binding!);const playing=run();await vi.waitFor(()=>expect(finish).toBeTypeOf('function'))
  state.status='cancelled';state.current_item=null;state.answer_binding=null;state.audio_delivery!.playback_allowed=false;state.audio_delivery!.control_revision++
  vi.mocked(localApi.activeExamSimulation).mockResolvedValue({exam:null})
  await run({turnId:`cancel-${sequence}`,currentQuestion:{kind:'exam',binding:owner}},{capability:'exam.cancel',result:structuredClone(state)})
  expect(capture).toHaveBeenLastCalledWith(null);expect(cancelLocalSpeech).toHaveBeenCalled()
  expect((await playing).playback?.status).toBe('paused');finish('finished');await Promise.resolve()
  expect(present).toHaveBeenLastCalledWith(expect.objectContaining({status:'cancelled',item:null}))
  expect(vi.mocked(localApi.petMediaPlayback).mock.calls.map(x=>x[1].event)).toEqual(['started'])
})
it('preparation of run A does not erase a pending exam answer owned by run B', async()=>{
  state.status='paused';state.delivery_status='preparing';state.answer_binding=null;state.audio_delivery!.playback_allowed=false;state.audio_delivery!.resource=null
  await run({currentQuestion:{kind:'exam',binding:{run_id:'run-B',position:1,item_id:'other',source_version:'other-v'}}})
  expect(capture).not.toHaveBeenCalled();expect(queueLocalPcm).not.toHaveBeenCalled()
})
it('ordinary submit uses its nested current exam binding and preserves other owners when no binding remains', async()=>{
  state.audio_delivery=null;state.current_item={...item('canonical-item'),answer_type:'text',pet_audio_modality:null}
  await run({turnId:`ordinary-${sequence}`},{capability:'exam.submit',result:{exam:structuredClone(state)}})
  expect(capture).toHaveBeenLastCalledWith(state.answer_binding)
  capture.mockClear();state.status='completed';state.answer_binding=null
  vi.mocked(localApi.activeExamSimulation).mockResolvedValue({exam:null})
  await run({turnId:`ordinary-done-${sequence}`,currentQuestion:{kind:'diagnostic',binding:{prompt_id:'original',item_version:'v'}}},{capability:'exam.submit',result:{exam:structuredClone(state)}})
  expect(capture).not.toHaveBeenCalled();expect(queueLocalPcm).not.toHaveBeenCalled()
})

it('projects shared listening last slot then the genuine mixed reading item/material, including an old cached cue, without replaying', async()=>{
  await run();state=projection(2);await run({turnId:`shared-last-${sequence}`},{capability:'exam.submit',result:{exam:structuredClone(state)}})
  const oldCue=structuredClone(state)
  vi.mocked(queueLocalPcm).mockClear();vi.mocked(localApi.petMediaControl).mockClear();vi.mocked(localApi.petMediaPlayback).mockClear();capture.mockClear();present.mockClear()
  state={...state,current_position:3,current_item:{...item('reading-item'),answer_type:'text',pet_audio_modality:null,prompt:'What does the library note ask?',stimulus_text:'Public synthetic note: please return the book on Friday.',stimulus_asset:'pet-study-room'},
    audio_delivery:null,answer_binding:{run_id:'exam-run',position:3,item_id:'reading-item',source_version:'reading-source-v3'}}
  await run({turnId:`reading-${sequence}`},{capability:'exam.submit',result:{exam:structuredClone(state)}})
  expect(present).toHaveBeenLastCalledWith(expect.objectContaining({position:3,item:state.current_item}))
  expect(capture).toHaveBeenLastCalledWith(state.answer_binding)
  await run({turnId:`old-listening-cache-${sequence}`},{result:oldCue,replayed:true,replay_audio_allowed:false})
  expect(present).toHaveBeenLastCalledWith(expect.objectContaining({position:3,item:state.current_item}))
  expect(present.mock.calls.every(([view])=>view.item?.diagnostic_item_id==='reading-item')).toBe(true)
  expect(queueLocalPcm).not.toHaveBeenCalled();expect(localApi.petMediaControl).not.toHaveBeenCalled();expect(localApi.petMediaPlayback).not.toHaveBeenCalled()
})
it.each(['item','source'])('a mixed current %s mismatch removes the stale question and performs no answer owner or playback action', async change=>{
  state={...state,current_position:3,current_item:{...item('reading-item'),answer_type:'text',pet_audio_modality:null,stimulus_text:'Public reading original'},
    audio_delivery:null,answer_binding:{run_id:'exam-run',position:3,item_id:'reading-item',source_version:'reading-v3'}}
  const expected=structuredClone(state)
  if(change==='item')state.answer_binding!.item_id='other-item'
  else state.answer_binding!.source_version='withdrawn-v4'
  await run({currentQuestion:{kind:'exam',binding:{run_id:'exam-run',position:2,item_id:'second-item',source_version:'slot-version-2'}}},{capability:'exam.submit',result:{exam:expected}})
  expect(present).toHaveBeenLastCalledWith(expect.objectContaining({item:null,message:expect.stringContaining('尚未核对')}))
  expect(capture).not.toHaveBeenCalled();expect(cancelLocalSpeech).not.toHaveBeenCalled();expect(localApi.petMediaControl).not.toHaveBeenCalled();expect(queueLocalPcm).not.toHaveBeenCalled()
})
it('terminal mixed status clears only its own view and cannot clear run B when no current answer owner exists', async()=>{
  state.status='completed';state.audio_delivery=null;state.current_item=null;state.answer_binding=null
  vi.mocked(localApi.activeExamSimulation).mockResolvedValue({exam:null})
  await run({currentViewRunId:'run-B'},{capability:'exam.submit',result:{exam:structuredClone(state)}})
  expect(present).not.toHaveBeenCalled();expect(capture).not.toHaveBeenCalled()
  await run({turnId:`terminal-${sequence}`,currentViewRunId:'exam-run'},{capability:'exam.submit',result:{exam:structuredClone(state)}})
  expect(present).toHaveBeenLastCalledWith(expect.objectContaining({runId:'exam-run',item:null,status:'completed'}));expect(capture).not.toHaveBeenCalled()
})
