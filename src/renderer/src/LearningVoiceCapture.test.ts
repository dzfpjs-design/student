// @vitest-environment node
import { afterEach, expect, it, vi } from 'vitest'
vi.mock('./listenReadyCue',()=>({requestListenReady:vi.fn(),requestListenReadyRecovery:vi.fn(),cancelListenReady:vi.fn()}))
vi.mock('./api',()=>({localApi:{}}))
vi.mock('./companionVoice',()=>({getCompanionPlaybackLease:()=>null,getCompanionVoiceState:()=> 'idle',subscribeCompanionVoice:()=>()=>{}}))
vi.mock('./voiceCaptureCoordinator',()=>({getVoiceCaptureOwner:()=>null,subscribeVoiceCapture:()=>()=>{}}))
vi.mock('./microphoneEchoCancellation',()=>({microphoneEchoMode:()=> 'unknown'}))
import { clearVoiceCaptureContext, voiceCaptureReference, voiceCaptureScopeStamp, voiceQuestionCaptureEpoch,
  trackVoiceLearningCapture, setVoiceQuestionCapture, setVoiceActivityCapture, setVoicePendingEvaluationCapture,
  setVoiceTaskChoiceCapture, setVoiceLocalRecoveryCapture, trackDiagnosticCapture } from './voiceCaptureContext'
import { beginOrdinaryResponse, registerOrdinarySession, revokeOrdinaryResponses } from './ordinaryResponseGrant'
import { realtimeInputSourceStatus } from './realtimeInputCapture'
const hint=(turn='t',child='c',session='s')=>({session_id:session,pending_learning_capture:{
  version:'ordinary_learning_capture_v1',session_id:session,child_id:child,
  reference:{kind:'learning_reply',binding:{objective_id:'o',plan_id:'p'+turn,source_agent_turn_id:turn,
    capability_name:turn==='t'?'learning.explain':'learning.evaluate_response',phase:'study',source_version:turn==='t'?'a'.repeat(64):'b'.repeat(64)}}}})
const read=(value=hint(),current=()=>true)=>trackVoiceLearningCapture('s','c',async()=>value,current)
let dispose:(()=>void)|undefined
afterEach(()=>{revokeOrdinaryResponses();dispose?.();dispose=undefined;clearVoiceCaptureContext()})
it('restores the actual runtime hint before constructing a response grant; same source does not retire it',async()=>{
  await read()
  const scope={sessionId:'s',childId:'c',state:'study'}
  dispose=registerOrdinarySession(scope)
  const grant=beginOrdinaryResponse({...scope,prompt:{},text:'Public synthetic prompt',sourceAgentTurnId:'t',isCurrent:()=>true})
  const stamp=voiceCaptureScopeStamp('s','c'),epoch=voiceQuestionCaptureEpoch('s')
  expect(grant.playbackCurrent()).toBe(true)
  // Property order is not a new source.
  const next=hint();next.pending_learning_capture.reference.binding=Object.fromEntries(Object.entries(next.pending_learning_capture.reference.binding).reverse()) as typeof next.pending_learning_capture.reference.binding
  await read(next)
  expect(voiceQuestionCaptureEpoch('s')).toBe(epoch)
  expect(voiceCaptureScopeStamp('s','c')).toEqual(stamp)
  expect(grant.playbackCurrent()).toBe(true)
  await read(hint('t2'))
  expect(grant.playbackCurrent()).toBe(false)
})
it('preserves the accepted onset while explain moves to evaluate; old onset never acquires the new reference',async()=>{
  await read()
  const onset={sessionId:'s',reference:voiceCaptureReference('s','c'),source:'accepted_onset' as const,
    carryoverConfirmationId:null,localInputId:'synthetic-onset',capturedAt:1}
  expect(realtimeInputSourceStatus(onset,'s','c')).toBe('current')
  await read(hint('t2'))
  expect(onset.reference.question_reference?.kind).toBe('learning_reply')
  expect(onset.reference.question_reference).toMatchObject({binding:{source_agent_turn_id:'t'}})
  expect(realtimeInputSourceStatus(onset,'s','c')).toBe('source_changed')
})
it.each(['question','activity','evaluation','choice','recovery'])('never steals the existing %s owner',async(owner)=>{
  await read()
  if(owner==='question')setVoiceQuestionCapture('s','c',{kind:'diagnostic',binding:{prompt_id:'d',item_version:'v'}})
  if(owner==='activity')setVoiceActivityCapture('s','c',{activity_id:'a',version:'v',last_turn_id:'t'})
  if(owner==='evaluation')setVoicePendingEvaluationCapture('s','c','e')
  if(owner==='choice')setVoiceTaskChoiceCapture('s','c',{offer_turn_id:'offer',source_version:'v'})
  if(owner==='recovery')setVoiceLocalRecoveryCapture('s','c',true)
  await read(hint('t2'))
  const ref=voiceCaptureReference('s','c')
  if(owner==='question')expect(ref.question_reference?.kind).toBe('diagnostic')
  else expect(ref.question_reference).toBeFalsy()
})
it('preserves legacy diagnostic tracking and release, and isolates children',async()=>{
  await read()
  await trackDiagnosticCapture('s',async()=>({status:'waiting_answer',prompt:{prompt_id:'d',item_version:'v',child_id:'c',origin_session_id:'s'}} as never))
  expect(voiceCaptureReference('s','c').question_reference?.kind).toBe('diagnostic')
  expect(voiceCaptureReference('s','other').question_reference).toBeNull()
})
it('latest concurrent GET wins and a late old response cannot retire its grant',async()=>{
  let finish!:(value:ReturnType<typeof hint>)=>void
  const old=trackVoiceLearningCapture('s','c',()=>new Promise<ReturnType<typeof hint>>(r=>{finish=r}),()=>true)
  await read(hint('t2'))
  dispose=registerOrdinarySession({sessionId:'s',childId:'c',state:'study'})
  const grant=beginOrdinaryResponse({sessionId:'s',childId:'c',state:'study',prompt:{},text:'Synthetic',isCurrent:()=>true})
  finish(hint())
  expect((await old).current).toBe(false)
  expect(voiceCaptureReference('s','c').question_reference).toMatchObject({binding:{source_agent_turn_id:'t2'}})
  expect(grant.playbackCurrent()).toBe(true)
})
it('unmount and clear retire pending publication',async()=>{
  let finish!:(value:ReturnType<typeof hint>)=>void
  const old=trackVoiceLearningCapture('s','c',()=>new Promise<ReturnType<typeof hint>>(r=>{finish=r}),()=>true)
  clearVoiceCaptureContext('s');finish(hint())
  expect((await old).current).toBe(false)
  expect(voiceCaptureReference('s','c').question_reference).toBeNull()
  expect((await read(hint(),()=>false)).current).toBe(false)
})
it.each(['error','wrong-session','wrong-child','extra','null'])('fails closed on %s and clears the old hint',async(mode)=>{
  await read()
  const bad=mode==='wrong-session'?hint('t2','c','other'):mode==='wrong-child'?hint('t2','other'):hint()
  if(mode==='extra')Object.assign(bad.pending_learning_capture.reference.binding,{extra:true})
  const result=await trackVoiceLearningCapture('s','c',async()=>{
    if(mode==='error')throw new Error('SYNTHETIC_HTTP_FAILURE')
    return mode==='null'?{session_id:'s',pending_learning_capture:null}:bad
  },()=>true)
  expect(result.current).toBe(true)
  expect(voiceCaptureReference('s','c').question_reference).toBeNull()
})

it('keeps the application request-response source closed and restores its exact reference',async()=>{
  const next=hint('request');next.pending_learning_capture.reference.binding.capability_name='learning.request_response'
  await read(next)
  expect(voiceCaptureReference('s','c').question_reference).toEqual(next.pending_learning_capture.reference)
  const epoch=voiceQuestionCaptureEpoch('s')
  await read(next);expect(voiceQuestionCaptureEpoch('s')).toBe(epoch)
  const bad=structuredClone(next);bad.pending_learning_capture.reference.binding.capability_name='learning.unregistered'
  await read(bad);expect(voiceCaptureReference('s','c').question_reference).toBeNull()
})
