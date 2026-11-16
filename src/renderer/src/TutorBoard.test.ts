import {ordinaryPromptPlayback,registerOrdinarySession,ordinaryResponseGrant,revokeOrdinaryResponses} from './ordinaryResponseGrant'
import * as voice from './companionVoice'
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
  ports = { speak: vi.fn(), cancel: vi.fn(), allowed: () => true, display: vi.fn(), error: vi.fn() }
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


function attach(){
 registerOrdinarySession({sessionId:'session',childId:'child',state:'study'})
 let voiceState:ReturnType<typeof voice.getCompanionVoiceState>='idle',voiceLease:ReturnType<typeof voice.getCompanionPlaybackLease>=null
 vi.spyOn(voice,'getCompanionVoiceState').mockImplementation(()=>voiceState);vi.spyOn(voice,'getCompanionPlaybackLease').mockImplementation(()=>voiceLease)
 let permit:ReturnType<typeof ordinaryPromptPlayback>|undefined
 ports.response=(checkpoint,text,current,sourceAgentTurnId)=>{permit=ordinaryPromptPlayback({sessionId:'session',childId:'child',state:'study',prompt:checkpoint,text,sourceAgentTurnId,sourceTutorTurnId:checkpoint.turn_id,deliveryPending:true,isCurrent:current});return permit}
 const start=(call:Parameters<typeof ports.speak>)=>{voiceState='speaking';voiceLease={id:'actual-segment',generation:1,sessionId:'session',kind:'local_sapi',text:call[0]};call[3]()}
 const end=(call:Parameters<typeof ports.speak>)=>{voiceLease=null;voiceState='idle';call[4].onPlaybackEnd?.()}
 return{permit:()=>permit,start,end}
}
afterEach(()=>revokeOrdinaryResponses())
it('actual board caller creates response only final segment and confirms after persisted endedACK',async()=>{
 const live=attach();await controller.present('turn',board,{localOnly:true});expect(live.permit()).toBeUndefined()
 const first=vi.mocked(ports.speak).mock.calls[0];live.start(first);live.end(first);await tick();await tick()
 expect(live.permit()).toBeDefined();expect(ordinaryResponseGrant('session','child')).toBeNull()
 const final=vi.mocked(ports.speak).mock.calls[1];live.start(final);live.end(final);await tick();await tick()
 expect(state?.status).toBe('playback_finished');expect(ordinaryResponseGrant('session','child')).toBe(live.permit()!.grant)
 expect(vi.mocked(localApi.mutateTutorPlayback).mock.calls.filter(x=>x[0]==='session'&&x[1]==='segment').map(x=>x[2].event)).toEqual(['started','ended','started','ended'])
})
it('lost final endedACK revokes early response and leaves the real saved journal pending',async()=>{
 const live=attach();await controller.present('turn',board,{localOnly:true});const first=vi.mocked(ports.speak).mock.calls[0];live.start(first);live.end(first);await tick();await tick()
 loseEnd=true;const final=vi.mocked(ports.speak).mock.calls[1];live.start(final);live.end(final);await tick();await tick()
 expect(ordinaryResponseGrant('session','child')).toBeNull();expect(localStorage.length).toBe(1);expect(ports.error).toHaveBeenCalledOnce()
})
it('actual interruption cannot use old same final callback to manufacture delivery',async()=>{
 const live=attach();await controller.present('turn',board,{localOnly:true});const first=vi.mocked(ports.speak).mock.calls[0];live.start(first);live.end(first);await tick();await tick()
 const final=vi.mocked(ports.speak).mock.calls[1];live.start(final);controller.interrupt();live.end(final);await tick();await tick()
 expect(ordinaryResponseGrant('session','child')).toBeNull();expect(state?.status).not.toBe('playback_finished')
})

it.each(['actual-agent',undefined])('actual final board marker retains existing Agent source without inventing Tutor-only source (%s)',async(sourceAgentTurnId)=>{
 const live=attach();await controller.present('turn',board,{localOnly:true,sourceAgentTurnId});const first=vi.mocked(ports.speak).mock.calls[0];live.start(first);live.end(first);await tick();await tick()
 const final=vi.mocked(ports.speak).mock.calls[1];live.start(final);live.end(final);await tick();await tick()
 expect(live.permit()!.grant.binding).toEqual({version:'ordinary_prompt_response_v1',session_state:'study',source_tutor_turn_id:'turn',...(sourceAgentTurnId?{source_agent_turn_id:sourceAgentTurnId}:{})})
})
