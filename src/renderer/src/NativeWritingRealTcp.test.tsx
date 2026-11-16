import fs from 'node:fs'
import { webcrypto } from 'node:crypto'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import type { ComponentProps } from 'react'
import * as inputs from './LocalInputs'
import { localApi, resetRuntimeCache } from './api'
import { SessionWorkspace } from './SessionWorkspace'
import { submitNativeWriting } from './nativeWritingFlow'
import { captureNativeWriting, signNativeWriting } from '../../main/native-writing'
import { ownedVoiceRuntimeConfig, ownedVoiceRuntimePolicyDesktop } from './test/ownedVoiceRuntimePolicy'

let microphone:ComponentProps<typeof inputs.HandsFreeVoiceInput>|undefined
vi.mock('./LocalInputs',async original=>({...await original<typeof import('./LocalInputs')>(),
  HandsFreeVoiceInput:(props:typeof microphone)=>{microphone=props;return null},VoiceInputButton:()=>null}))
vi.mock('./ContinuousDocumentMonitor',()=>({ContinuousDocumentMonitor:()=>null}))
vi.mock('./DocumentCameraCapture',()=>({DocumentCameraCapture:()=>null}))
vi.mock('./VoiceCalibrationPanel',()=>({VoiceCalibrationPanel:()=>null}))
afterEach(()=>{cleanup();microphone=undefined;resetRuntimeCache();localStorage.clear();vi.restoreAllMocks();vi.unstubAllGlobals()})

it.skipIf(!process.env.HSC_NATIVE_TCP_CASE)('connects the real native writing flow and child handlers to actual TCP after process restart',async()=>{
  const config=JSON.parse(fs.readFileSync(process.env.HSC_NATIVE_TCP_CASE!,'utf8'))
  const {body}=config
  const scope={child_id:body.child_id,session_id:body.session_id,prompt_id:body.prompt_id,
    item_version:body.item_version,diagnostic_item_id:body.diagnostic_item_id}
  expect(Number.isSafeInteger(config.runtime_epoch)&&config.runtime_epoch>0).toBe(true)
  expect(typeof config.session_token==='string'&&config.session_token.length>0).toBe(true)
  const runtime=ownedVoiceRuntimeConfig(config.base_url,config.session_token,config.runtime_epoch)
  const nativeFetch=globalThis.fetch
  const capture=vi.fn(input=>captureNativeWriting(input,runtime,(value,time)=>signNativeWriting(config.key,value,time)))
  vi.stubGlobal('crypto',webcrypto)
  vi.stubGlobal('desktop',ownedVoiceRuntimePolicyDesktop({captureNativeWritingOriginal:capture,
    getWakeWordSettings:async()=>({childId:body.child_id,wakeWord:'小知',enabled:true,inactivitySeconds:60}),
    getVoiceOnboarding:async()=>({childId:body.child_id,version:1,step:'completed'})},runtime,nativeFetch))
  vi.spyOn(inputs,'speakLocally').mockImplementation((_text,_session,done)=>{done?.('text_only')})
  vi.spyOn(inputs,'cancelLocalSpeech').mockImplementation(()=>undefined)
  resetRuntimeCache()
  const wrong=ownedVoiceRuntimeConfig(config.base_url,config.session_token+'-wrong',config.runtime_epoch)
  const denied=await nativeFetch(config.base_url+'/v1/private/money-egress',{method:'GET',redirect:'error',cache:'no-store',
    signal:AbortSignal.timeout(2000),headers:{'X-HSC-Session':wrong.sessionToken}})
  expect(denied.status).toBe(401)
  await denied.body?.cancel()
  expect(await ownedVoiceRuntimePolicyDesktop({},wrong,nativeFetch).getVoiceRuntimePolicy()).toEqual({version:1,capture:'blocked',runtimeEpoch:config.runtime_epoch})
  expect(await window.desktop?.getVoiceRuntimePolicy?.()).toEqual({version:1,capture:'configured',runtimeEpoch:config.runtime_epoch})
  if(config.phase==='saved_capture') {
    await localApi.saveSessionDraft(body.session_id,'study',{native_writing_draft:{input_kind:'typed',scope,text:body.text}})
    await expect(submitNativeWriting({scope,text:body.text,isCurrent:()=>true,capture,
      answer:values=>localApi.answerDiagnosticPrompt(body.session_id,values)})).rejects.toThrow()
    expect(capture).toHaveBeenCalledTimes(1)
    expect(JSON.stringify(localStorage)).toContain('answer_unknown')
    fs.writeFileSync(config.journal,JSON.stringify({...localStorage}))
    fs.writeFileSync(config.result,JSON.stringify({capture_posts:1,answer_posts:1,unknown_preserved:true}))
    return
  }
  if(config.phase==='capture') {
    await localApi.saveSessionDraft(body.session_id,'study',{native_writing_draft:{input_kind:'typed',scope,text:body.text}})
    const fetch=globalThis.fetch
    vi.stubGlobal('fetch',(url:RequestInfo|URL,init?:RequestInit)=>fetch(url,String(url).endsWith('/native-writing-original')
      ?{...init,headers:{...init?.headers,'X-Synthetic-Receipt-Loss':'1'}}:init))
    await expect(submitNativeWriting({scope,text:body.text,isCurrent:()=>true,capture,
      answer:values=>localApi.answerDiagnosticPrompt(body.session_id,values)})).rejects.toThrow()
    expect(capture).toHaveBeenCalledTimes(1)
    expect(JSON.stringify(localStorage)).toContain('capture_unknown')
    fs.writeFileSync(config.journal,JSON.stringify({...localStorage}))
    fs.writeFileSync(config.result,JSON.stringify({capture_posts:1,unknown_preserved:true}))
    return
  }
  for(const [key,value] of Object.entries(JSON.parse(fs.readFileSync(config.journal,'utf8'))))localStorage.setItem(key,value as string)
  const initial=await localApi.sessionWorkspace(body.session_id)
  expect(initial.drafts.study?.payload.native_writing_draft).toMatchObject({scope,text:body.text,input_kind:'typed'})
  const answer=vi.spyOn(localApi,'answerDiagnosticPrompt')
  const read=vi.spyOn(localApi,'nativeWritingReceipt')
  const semantic=vi.spyOn(localApi,'agentTurn')
  const resume=vi.spyOn(localApi,'nativeWritingResume')
  if(config.phase==='saved_resume') {
    const fetch=globalThis.fetch
    vi.stubGlobal('fetch',(url:RequestInfo|URL,init?:RequestInit)=>fetch(url,String(url).endsWith('/native-writing-resume')
      ?{...init,headers:{...init?.headers,'X-Synthetic-Receipt-Loss':'1'}}:init))
  }
  await act(async()=>{render(<SessionWorkspace initial={initial} childName="合成孩子" childGrade="四年级" onBack={()=>undefined}/>)})
  await waitFor(()=>expect(microphone?.onTranscript).toBeTypeOf('function'),{timeout:10000})
  if(config.phase==='saved_read') {
    await act(async()=>{await microphone!.onTranscript('刚才作文交上了吗')})
    expect(read).toHaveBeenCalledTimes(1);expect(answer).not.toHaveBeenCalled();expect(capture).not.toHaveBeenCalled();expect(resume).not.toHaveBeenCalled()
    expect(JSON.stringify(localStorage)).toContain('answered')
    const q=read.mock.calls[0][1]
    const result=await localApi.nativeWritingReceipt(body.session_id,q)
    expect(result.resume?.client_action_id).toBe(q.resume_action_id)
    expect(result.result?.attempt?.submitted_answer).toBe(body.text)
    fs.writeFileSync(config.result,JSON.stringify({capture_posts:0,answer_posts:0,resume_posts:0,readonly_recovered:true,
      resume_action_id:q.resume_action_id,artifact_id:result.original?.artifact_id,attempt_id:result.result?.attempt?.attempt_id}))
    return
  }
  await waitFor(()=>expect((screen.getByLabelText('输入挑战题答案') as HTMLTextAreaElement).value).toBe(body.text),{timeout:10000})
  await act(async()=>{await microphone!.onTranscript('刚才作文交上了吗')})
  expect(read).toHaveBeenCalledTimes(1);expect(answer).not.toHaveBeenCalled();expect(capture).not.toHaveBeenCalled()
  expect(JSON.stringify(localStorage)).toContain(config.phase==='saved_resume'?'answer_saved':'captured')
  await act(async()=>{await microphone!.onTranscript('继续提交本题')})
  expect(semantic).toHaveBeenCalledTimes(2)
  if(config.phase==='saved_resume') {
    expect(answer).not.toHaveBeenCalled();expect(capture).not.toHaveBeenCalled();expect(resume).toHaveBeenCalledTimes(1)
    expect(resume.mock.calls[0][1].target.saved_answer).toBeTruthy()
    expect(JSON.stringify(localStorage)).toContain('resume_unknown')
    fs.writeFileSync(config.journal,JSON.stringify({...localStorage}))
    fs.writeFileSync(config.result,JSON.stringify({capture_posts:0,answer_posts:0,resume_posts:1,
      resume_action_id:resume.mock.calls[0][1].client_action_id,exact_saved_target:resume.mock.calls[0][1].target}))
    return
  }
  expect(answer).toHaveBeenCalledTimes(1)
  expect(answer.mock.calls[0][1].answer).toBe(body.text)
  expect(capture).not.toHaveBeenCalled()
  const record=await localApi.nativeWritingReceipt(body.session_id,{...scope,
    text_sha256:Buffer.from(await webcrypto.subtle.digest('SHA-256',Buffer.from(body.text))).toString('hex'),
    capture_action_id:`native-writing:${scope.prompt_id}:${Buffer.from(await webcrypto.subtle.digest('SHA-256',Buffer.from(body.text))).toString('hex')}`,
    answer_action_id:answer.mock.calls[0][1].client_action_id})
  expect(record.state).toBe('answered')
  expect(record.result?.attempt?.submitted_answer).toBe(body.text)
  fs.writeFileSync(config.result,JSON.stringify({semantic_requests:2,answer_posts:1,capture_posts:0,
    original_artifact_id:record.original?.artifact_id,attempt_id:record.result?.attempt?.attempt_id,
    exact_original_text:true,control_utterance_not_answer:true,query_reposts:0}))
},60000)
