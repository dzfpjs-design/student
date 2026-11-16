import {afterEach,expect,it} from 'vitest'
import {ordinaryResponseMayOwnPrompt} from './ordinaryResponseGrant'
import {clearVoiceCaptureContext,publishVoiceDocumentCapture,setVoiceActivityCapture,setVoiceLocalRecoveryCapture,setVoicePendingEvaluationCapture,setVoiceQuestionCapture,setVoiceTaskChoiceCapture} from './voiceCaptureContext'
import type {VoiceQuestionReference} from './voiceCaptureContext'
import type {DocumentPageRevision} from './types'
const session='synthetic-boundary',child='synthetic-child'
afterEach(()=>clearVoiceCaptureContext())
it('permits a plain prompt without dedicated transport authority',()=>expect(ordinaryResponseMayOwnPrompt(session,child)).toBe(true))
it.each(['diagnostic','diagnostic_help','exam','vocabulary','mistake','document'] as const)('preserves current %s question protocol',kind=>{
 setVoiceQuestionCapture(session,child,{kind,binding:{prompt_id:'p',item_version:'v1'}} as VoiceQuestionReference)
 expect(ordinaryResponseMayOwnPrompt(session,child)).toBe(false)
})
it.each(['activity','choice','pending','recovery'] as const)('preserves current %s protocol',kind=>{
 if(kind==='activity')setVoiceActivityCapture(session,child,{activity_id:'a',version:'v1',last_turn_id:'turn'})
 if(kind==='choice')setVoiceTaskChoiceCapture(session,child,{offer_turn_id:'turn',source_version:'v1'})
 if(kind==='pending')setVoicePendingEvaluationCapture(session,child,'turn')
 if(kind==='recovery')setVoiceLocalRecoveryCapture(session,child,true)
 expect(ordinaryResponseMayOwnPrompt(session,child)).toBe(false)
})
it('does not reinterpret protected PCM as ordinary speech',()=>expect(ordinaryResponseMayOwnPrompt(session,child,{pcmControlReference:{} as never})).toBe(false))
it('preserves an actual retained document fallback question',()=>{
 const owner=publishVoiceDocumentCapture(session,child,{session_id:session,child_id:child,revision_id:'r',artifact_id:'a',
  content_sha256:'1'.repeat(64),extracted_text:'Public synthetic page',page_source:{version:'document_page_source_v1',status:'retained',
   sha256:'2'.repeat(64),byte_length:1,media_type:'image/png',encrypted:true,local_only:true,
   cloud_transmission_allowed:false,retention_reason:'accepted_document_page_revision'}} as unknown as DocumentPageRevision)
 expect(owner).toEqual(expect.any(Symbol))
 expect(ordinaryResponseMayOwnPrompt(session,child)).toBe(false)
})
