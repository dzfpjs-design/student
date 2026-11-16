import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { createHash } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { SessionWorkspace } from './SessionWorkspace'
import type { SessionWorkspaceState } from './types'
import { requestCameraDevice, queryCameraDevices } from './cameraDeviceRuntime'
import * as media from './mediaDevices'
import * as LocalInputs from './LocalInputs'
import { reconcileObservationRegistration } from './observationCameraStartup'
import { reconcileObservationCoverage } from './observationCoverageStartup'
vi.mock('./QwenRealtimeVoiceInput', () => ({ QwenRealtimeVoiceInput: () => null }))
const initialWorkspace: SessionWorkspaceState = {
  session: {
    session_id: 'session-draft-1',
    child_id: 'child-junior-3',
    status: 'active',
    state: 'classroom_recall',
    started_at: '2026-08-26T10:00:00Z',
    last_checkpoint_at: '2026-08-26T10:00:00Z',
    observation_revision: 0, observation_generation: 'a'.repeat(64), observation_status: 'paused',
    snooze_until: null,
    recall_declared_empty: 0,
    homework_declared_empty: 0
  },
  family_rule: {
    child_id: 'child-junior-3',
    latest_end_time: '21:30',
    max_learning_minutes: 120,
    task_segment_minutes: 25,
    break_minutes: 5,
    pet_min_minutes: 0,
    competition_minutes: 0,
    observation_mode: 'shadow',
    reminder_budget_per_10m: 1,
    misclassification_shadow_threshold: 3,
    version: 1,
    updated_at: '2026-08-26T10:00:00Z'
  },
  planning_context: {
    energy_level: 'medium',
    fixed_commitment_minutes: 0,
    fixed_commitment_note: null,
    tomorrow_event_subject: null,
    tomorrow_event_title: null,
    tomorrow_event_minutes: 0,
    updated_at: '2026-08-26T10:00:00Z'
  },
  recalls: [],
  homework: [],
  carryover_candidates: [],
  carryover_decisions: [],
  plan: null,
  plan_items: [],
  daily_report_ready: false,
  latest_tutor_turn: null,
  sensor_release: {
    opened_leases: 0,
    active_leases: 0,
    released_leases: 0,
    application_release_confirmed: false,
    evidence_level: 'not_opened',
    os_release_verified: false,
    release_verification: null,
    by_device: {}
  },
  drafts: {
    classroom_recall: {
      payload: { subject: '数学', summary: '今天讲了相似三角形' },
      updated_at: '2026-08-26T10:03:00Z'
    }
  }
}

const receipt = (epoch: string, boundary: string) => ({status:'recorded',event_id:`coverage-action:${createHash('sha256').update(`coverage-${boundary}:${epoch}`).digest('hex')}`})
describe('authoritative observation resume recovery', () => {
 let paused = false, frames = 20, time = 1
 let stops: Array<ReturnType<typeof vi.fn>> = []
 let tracks: Array<{readyState:string;enabled:boolean;muted:boolean}> = []
 const originalMedia = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices')
 const originalQuality = Object.getOwnPropertyDescriptor(HTMLVideoElement.prototype, 'getVideoPlaybackQuality')
 beforeEach(() => {
  vi.useFakeTimers();paused=false;frames=20;time=1;stops=[];tracks=[];localStorage.clear()
  vi.spyOn(crypto.subtle,'digest').mockImplementation(async (_a,data)=>Uint8Array.from(createHash('sha256').update(Buffer.from(data as ArrayBuffer)).digest()).buffer)
  vi.spyOn(HTMLMediaElement.prototype,'play').mockImplementation(async()=>{paused=false})
  vi.spyOn(HTMLMediaElement.prototype,'paused','get').mockImplementation(()=>paused)
  vi.spyOn(HTMLMediaElement.prototype,'ended','get').mockReturnValue(false)
  vi.spyOn(HTMLMediaElement.prototype,'readyState','get').mockReturnValue(2)
  vi.spyOn(HTMLMediaElement.prototype,'currentTime','get').mockImplementation(()=>time)
  vi.spyOn(HTMLVideoElement.prototype,'videoWidth','get').mockReturnValue(640)
  vi.spyOn(HTMLVideoElement.prototype,'videoHeight','get').mockReturnValue(360)
  Object.defineProperty(HTMLVideoElement.prototype,'getVideoPlaybackQuality',{configurable:true,value:()=>({totalVideoFrames:frames+Math.floor(performance.now()/25),droppedVideoFrames:0})})
  vi.spyOn(HTMLCanvasElement.prototype,'getContext').mockReturnValue(null)
  Object.defineProperty(navigator,'mediaDevices',{configurable:true,value:{enumerateDevices:vi.fn().mockResolvedValue([]),getUserMedia:vi.fn(async()=>{const track={readyState:'live',enabled:true,muted:false,stop:vi.fn(()=>{track.readyState='ended'}),addEventListener:vi.fn()};stops.push(track.stop);tracks.push(track);return {getTracks:()=>[track],getVideoTracks:()=>[track]}})}})
  vi.spyOn(media,'resolveBoundVideoDevice').mockResolvedValue({device:{deviceId:'bound-camera'} as MediaDeviceInfo,method:'system_id_hash'})
  vi.spyOn(media,'mediaDeviceRegistration').mockResolvedValue({systemIdHash:'camera-hash',capabilities:{}})
  vi.spyOn(localApi,'deviceBindings').mockResolvedValue([{device_type:'camera',system_id_hash:'camera-hash',label:'camera'}] as never)
  vi.spyOn(localApi,'resumeObservation').mockResolvedValue({...initialWorkspace.session,observation_status:'enabled'})
  vi.spyOn(localApi,'pauseObservation').mockResolvedValue({...initialWorkspace.session,observation_status:'paused'})
  vi.spyOn(localApi,'sessionWorkspace').mockResolvedValue({...initialWorkspace,session:{...initialWorkspace.session,observation_status:'enabled'}})
  vi.spyOn(localApi,'activityVoiceFocus').mockResolvedValue({status:'none'} as never)
  vi.spyOn(localApi,'registerDevice').mockResolvedValue({lease_id:'camera-lease',status:'active',expires_at:'later'})
  vi.spyOn(localApi,'releaseDeviceLease').mockImplementation(async leaseId=>({lease_id:leaseId,status:'released'}))
  vi.spyOn(localApi,'startObservationQualification').mockResolvedValue({} as never)
  vi.spyOn(localApi,'confirmDeviceFrame').mockImplementation(async leaseId=>({lease_id:leaseId,status:'frame_confirmed',resolved_incidents:0}))
  vi.spyOn(localApi,'beginObservationStream').mockImplementation(async (_s,epoch)=>receipt(epoch,'start'))
  vi.spyOn(localApi,'endObservationStream').mockImplementation(async (_s,epoch)=>receipt(epoch,'stop'))
  vi.spyOn(localApi,'recordDeviceIncident').mockResolvedValue({} as never)
  vi.spyOn(localApi,'pendingSessionBehaviorIntervention').mockResolvedValue({status:'none'} as never)
  vi.spyOn(LocalInputs,'speakLocally').mockImplementation(()=>{})
  vi.stubGlobal('desktop',{getQwenRealtimeSettings:async()=>({configured:true,enabled:true,endpoint:'synthetic-invalid',model:'qwen3.8-omni-flash-realtime',voice:'synthetic'})})
  vi.stubGlobal('fetch',vi.fn().mockRejectedValue(new Error('UNMOCKED_NETWORK')))
 })
 afterEach(async()=>{
  if(vi.isMockFunction(localApi.releaseDeviceLease))vi.mocked(localApi.releaseDeviceLease).mockImplementation(async id=>({lease_id:id,status:'released'}))
  cleanup();await act(async()=>{await vi.advanceTimersByTimeAsync(5100)})
  await act(async()=>{await reconcileObservationRegistration('camera-hash');await reconcileObservationCoverage(initialWorkspace.session.session_id)})
  vi.restoreAllMocks();vi.unstubAllGlobals();vi.useRealTimers();localStorage.clear()
  if(originalMedia)Object.defineProperty(navigator,'mediaDevices',originalMedia)
  if(originalQuality)Object.defineProperty(HTMLVideoElement.prototype,'getVideoPlaybackQuality',originalQuality)
  else delete (HTMLVideoElement.prototype as unknown as {getVideoPlaybackQuality?:unknown}).getVideoPlaybackQuality
 })
 const mount = () => render(<SessionWorkspace initial={initialWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
 it('releases and reconnects a stalled observation stream when the system clock moves backwards',async()=>{
  mount();await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  const opening=requestCameraDevice(initialWorkspace.session.session_id,'camera','resume')
  await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  await expect(opening).resolves.toMatchObject({device_status:'active'})
  expect(queryCameraDevices(initialWorkspace.session.session_id,'camera').devices[0].frame_confirmed).toBe(true)
  paused=true
  vi.setSystemTime(Date.now()-3_600_000)
  await act(async()=>{await vi.advanceTimersByTimeAsync(6_000)})
  expect(stops[0]).toHaveBeenCalledTimes(1)
  await act(async()=>{await vi.advanceTimersByTimeAsync(2_500)})
  expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
 })
 it('resumes initial paused authority then automatically reconnects paused video without a parent test rerender',async()=>{
  mount();await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled()
  const opening=requestCameraDevice(initialWorkspace.session.session_id,'camera','resume')
  await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  await expect(opening).resolves.toMatchObject({device_status:'active'})
  expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
  paused=true
  for(let i=0;i<6;i++){frames+=10;time++;await act(async()=>{await vi.advanceTimersByTimeAsync(1000)})}
  expect(stops[0]).toHaveBeenCalledTimes(1)
  await act(async()=>{await vi.advanceTimersByTimeAsync(2500)})
  expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
  expect(queryCameraDevices(initialWorkspace.session.session_id,'camera').devices[0]).toMatchObject({track_live:true,frame_confirmed:true})
 })
 it.each(['pause','privacy','cancel','owner'] as const)('rejects late enabled authority after %s without reopening',async boundary=>{
  let finish!: (v:SessionWorkspaceState)=>void
  vi.mocked(localApi.sessionWorkspace).mockReturnValueOnce(new Promise(resolve=>{finish=resolve}))
  const view=mount(), controller=new AbortController()
  await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  const result=requestCameraDevice(initialWorkspace.session.session_id,'camera','resume',controller.signal).catch(error=>error)
  await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  expect(localApi.sessionWorkspace).toHaveBeenCalledTimes(1)
  expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
  if(boundary==='pause'){
   const pausing=requestCameraDevice(initialWorkspace.session.session_id,'camera','pause')
   await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
   await expect(pausing).resolves.toMatchObject({device_status:'paused'})
  }else if(boundary==='privacy')await act(async()=>{window.dispatchEvent(new CustomEvent('hsc-privacy-pause'));await vi.advanceTimersByTimeAsync(100)})
  else if(boundary==='cancel')await act(async()=>{controller.abort();await vi.advanceTimersByTimeAsync(100)})
  else {view.unmount();await act(async()=>{await vi.advanceTimersByTimeAsync(100)});render(<SessionWorkspace initial={{...initialWorkspace,session:{...initialWorkspace.session,session_id:'new-camera-owner',child_id:'child-primary-4'}}} childName="新同学" childGrade="小学" onBack={vi.fn()}/>)}
  await act(async()=>{finish({...initialWorkspace,session:{...initialWorkspace.session,observation_status:'enabled'}});await vi.advanceTimersByTimeAsync(100)})
  if(boundary==='cancel')expect(await result).toHaveProperty('name','AbortError')
  else expect(await result).toBeInstanceOf(Error)
  await act(async()=>{await vi.advanceTimersByTimeAsync(9000)})
  expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
  expect(stops[0]).toHaveBeenCalledTimes(1)
 })
 it.each(['paused','child','session','completed'] as const)('requires fresh matching enabled authority rather than only a resolved refresh: %s',async mismatch=>{
  const session:SessionWorkspaceState['session']={...initialWorkspace.session,observation_status:'enabled'}
  if(mismatch==='paused')session.observation_status='paused'
  if(mismatch==='child')session.child_id='child-primary-4'
  if(mismatch==='session')session.session_id='foreign-camera-session'
  if(mismatch==='completed')session.status='completed'
  vi.mocked(localApi.sessionWorkspace).mockResolvedValueOnce({...initialWorkspace,session})
  mount();await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  const result=requestCameraDevice(initialWorkspace.session.session_id,'camera','resume').catch(error=>error)
  await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  expect(await result).toBeInstanceOf(Error)
  expect(stops[0]).toHaveBeenCalledTimes(1)
  await act(async()=>{await vi.advanceTimersByTimeAsync(9000)})
  expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
 })
 it('does not let an old refresh response or catch release the newer resumed stream',async()=>{
  let finish!: (v:SessionWorkspaceState)=>void
  vi.mocked(localApi.sessionWorkspace).mockReturnValueOnce(new Promise(resolve=>{finish=resolve}))
  mount();await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  const old=requestCameraDevice(initialWorkspace.session.session_id,'camera','resume').catch(error=>error)
  await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  const pausedResult=requestCameraDevice(initialWorkspace.session.session_id,'camera','pause')
  await act(async()=>{await vi.advanceTimersByTimeAsync(100)});await pausedResult
  const fresh=requestCameraDevice(initialWorkspace.session.session_id,'camera','resume')
  await act(async()=>{await vi.advanceTimersByTimeAsync(100)});await expect(fresh).resolves.toMatchObject({device_status:'active'})
  expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
  await act(async()=>{finish({...initialWorkspace,session:{...initialWorkspace.session,observation_status:'enabled'}});await vi.advanceTimersByTimeAsync(100)})
  expect(await old).toBeInstanceOf(Error)
  expect(stops[0]).toHaveBeenCalledTimes(1);expect(stops[1]).not.toHaveBeenCalled()
  expect(queryCameraDevices(initialWorkspace.session.session_id,'camera').devices[0]).toMatchObject({track_live:true,frame_confirmed:true})
 })
 it('rejects a late resume API response after privacy release before starting any capture',async()=>{
  let finish!: (v:typeof initialWorkspace.session)=>void
  vi.mocked(localApi.resumeObservation).mockReturnValueOnce(new Promise(resolve=>{finish=resolve}))
  mount();await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  const result=requestCameraDevice(initialWorkspace.session.session_id,'camera','resume').catch(error=>error)
  await act(async()=>{await vi.advanceTimersByTimeAsync(100);window.dispatchEvent(new CustomEvent('hsc-privacy-pause'))})
  await act(async()=>{finish({...initialWorkspace.session,observation_status:'enabled'});await vi.advanceTimersByTimeAsync(100)})
  expect(await result).toBeInstanceOf(Error)
  expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled()
  expect(localApi.sessionWorkspace).not.toHaveBeenCalled()
 })
 it('joins the enabled automatic opening when resume arrives before real GUM settles',async()=>{
  const acquire=vi.mocked(navigator.mediaDevices.getUserMedia).getMockImplementation()!
  let finish!:()=>void
  vi.mocked(navigator.mediaDevices.getUserMedia).mockImplementationOnce(constraints=>new Promise<void>(resolve=>{finish=resolve}).then(()=>acquire(constraints)))
  render(<SessionWorkspace initial={{...initialWorkspace,session:{...initialWorkspace.session,observation_status:'enabled'}}} childName="小辰" childGrade="初三" onBack={vi.fn()}/> )
  await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
  const result=requestCameraDevice(initialWorkspace.session.session_id,'camera','resume').catch(error=>error)
  await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  await act(async()=>{finish();await vi.advanceTimersByTimeAsync(100)})
  expect(await result).toMatchObject({device_status:'active',frame_confirmed:true,lease_confirmed:true})
  expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
  expect(localApi.registerDevice).toHaveBeenCalledTimes(1)
  expect(localApi.beginObservationStream).toHaveBeenCalledTimes(2)
  const originalEpoch=vi.mocked(localApi.beginObservationStream).mock.calls[0][1]
  const resumedEpoch=vi.mocked(localApi.beginObservationStream).mock.calls[1][1]
  expect(resumedEpoch).not.toBe(originalEpoch)
  expect(localApi.endObservationStream).toHaveBeenCalledWith(initialWorkspace.session.session_id,originalEpoch,0,'context_changed',expect.any(AbortSignal))
  expect(stops[0]).not.toHaveBeenCalled()
 })
 it('joins the enabled opening through deferred registration before the original frame wait',async()=>{
  let finish!:(lease:Awaited<ReturnType<typeof localApi.registerDevice>>)=>void
  vi.mocked(localApi.registerDevice).mockReturnValueOnce(new Promise(resolve=>{finish=resolve}))
  render(<SessionWorkspace initial={{...initialWorkspace,session:{...initialWorkspace.session,observation_status:'enabled'}}} childName="小辰" childGrade="初三" onBack={vi.fn()}/> )
  await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  expect(localApi.registerDevice).toHaveBeenCalledTimes(1)
  const result=requestCameraDevice(initialWorkspace.session.session_id,'camera','resume').catch(error=>error)
  await act(async()=>{await vi.advanceTimersByTimeAsync(5500)})
  await act(async()=>{finish({lease_id:'camera-lease',status:'active',expires_at:'later'});await vi.advanceTimersByTimeAsync(100)})
  expect(await result).toMatchObject({device_status:'active',frame_confirmed:true,lease_confirmed:true})
  expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
  expect(stops[0]).not.toHaveBeenCalled()
  expect(localApi.releaseDeviceLease).not.toHaveBeenCalled()
 })
 it('does not acknowledge resume before the joined automatic qualification completes',async()=>{
  let finish!:(qualification:Awaited<ReturnType<typeof localApi.startObservationQualification>>)=>void
  vi.mocked(localApi.startObservationQualification).mockReturnValueOnce(new Promise(resolve=>{finish=resolve}))
  render(<SessionWorkspace initial={{...initialWorkspace,session:{...initialWorkspace.session,observation_status:'enabled'}}} childName="小辰" childGrade="初三" onBack={vi.fn()}/> )
  await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  expect(localApi.startObservationQualification).toHaveBeenCalledTimes(1)
  let settled=false
  const result=requestCameraDevice(initialWorkspace.session.session_id,'camera','resume').then(value=>{settled=true;return value},error=>{settled=true;return error})
  await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  const beforeQualification=settled
  await act(async()=>{finish({} as never);await vi.advanceTimersByTimeAsync(100)})
  expect(beforeQualification).toBe(false)
  expect(await result).toMatchObject({device_status:'active'})
  expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
  expect(stops[0]).not.toHaveBeenCalled()
 })
 it.each(['pause','privacy','cancel','unmount','owner'] as const)('invalidates a joined pending GUM opening at %s without accepting the late stream',async boundary=>{
  const acquire=vi.mocked(navigator.mediaDevices.getUserMedia).getMockImplementation()!
  let finish!:()=>void
  vi.mocked(navigator.mediaDevices.getUserMedia).mockImplementationOnce(constraints=>new Promise<void>(resolve=>{finish=resolve}).then(()=>acquire(constraints)))
  const view=render(<SessionWorkspace initial={{...initialWorkspace,session:{...initialWorkspace.session,observation_status:'enabled'}}} childName="小辰" childGrade="初三" onBack={vi.fn()}/> )
  await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  const controller=new AbortController()
  const result=requestCameraDevice(initialWorkspace.session.session_id,'camera','resume',controller.signal).catch(error=>error)
  await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  if(boundary==='pause'){
   const pausing=requestCameraDevice(initialWorkspace.session.session_id,'camera','pause')
   await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
   await expect(pausing).resolves.toMatchObject({device_status:'paused',lease_confirmed:true})
  }else if(boundary==='privacy')await act(async()=>{window.dispatchEvent(new CustomEvent('hsc-privacy-pause'));await vi.advanceTimersByTimeAsync(100)})
  else if(boundary==='cancel')await act(async()=>{controller.abort();await vi.advanceTimersByTimeAsync(100)})
  else {
   view.unmount()
   if(boundary==='owner')render(<SessionWorkspace initial={{...initialWorkspace,session:{...initialWorkspace.session,session_id:'new-opening-owner',child_id:'child-primary-4'}}} childName="新同学" childGrade="小学" onBack={vi.fn()}/> )
   await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  }
  await act(async()=>{finish();await vi.advanceTimersByTimeAsync(100)})
  if(boundary==='cancel')expect(await result).toHaveProperty('name','AbortError')
  else expect(await result).toBeInstanceOf(Error)
  await act(async()=>{await vi.advanceTimersByTimeAsync(9000)})
  expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
  expect(stops[0]).toHaveBeenCalledTimes(1)
  expect(localApi.registerDevice).not.toHaveBeenCalled()
  expect(localApi.beginObservationStream).not.toHaveBeenCalled()
  expect(localApi.sessionWorkspace).not.toHaveBeenCalled()
 })
 it.each(['pause','unmount'] as const)('retires the joined registration at %s and releases its original late ACK',async boundary=>{
  let finish!:(lease:Awaited<ReturnType<typeof localApi.registerDevice>>)=>void
  vi.mocked(localApi.registerDevice).mockReturnValueOnce(new Promise(resolve=>{finish=resolve}))
  const view=render(<SessionWorkspace initial={{...initialWorkspace,session:{...initialWorkspace.session,observation_status:'enabled'}}} childName="小辰" childGrade="初三" onBack={vi.fn()}/> )
  await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  const result=requestCameraDevice(initialWorkspace.session.session_id,'camera','resume').catch(error=>error)
  await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  let pausing:ReturnType<typeof requestCameraDevice>|null=null
  if(boundary==='pause')pausing=requestCameraDevice(initialWorkspace.session.session_id,'camera','pause')
  else view.unmount()
  await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  await act(async()=>{finish({lease_id:'original-late-camera-lease',status:'active',expires_at:'later'});await vi.advanceTimersByTimeAsync(100)})
  if(pausing)await expect(pausing).resolves.toMatchObject({device_status:'paused',lease_confirmed:true})
  expect(await result).toBeInstanceOf(Error)
  expect(localApi.releaseDeviceLease).toHaveBeenCalledTimes(1)
  expect(vi.mocked(localApi.releaseDeviceLease).mock.calls[0][0]).toBe('original-late-camera-lease')
  expect(stops[0]).toHaveBeenCalledTimes(1)
  expect(localApi.beginObservationStream).not.toHaveBeenCalled()
  expect(localApi.sessionWorkspace).not.toHaveBeenCalled()
  await act(async()=>{await vi.advanceTimersByTimeAsync(9000)})
  expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
 })
 it('keeps the already enabled automatic path and successful resume from opening a duplicate stream',async()=>{
  render(<SessionWorkspace initial={{...initialWorkspace,session:{...initialWorkspace.session,observation_status:'enabled'}}} childName="小辰" childGrade="初三" onBack={vi.fn()}/> )
  await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  const result=requestCameraDevice(initialWorkspace.session.session_id,'camera','resume')
  await act(async()=>{await vi.advanceTimersByTimeAsync(100)});await expect(result).resolves.toMatchObject({device_status:'active'})
  expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
  expect(stops[0]).not.toHaveBeenCalled()
 })
 it('UI resume refreshes paused authority and reconnects after a paused video fault',async()=>{
  const view=mount();await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  fireEvent.click(view.getByRole('button',{name:'恢复本地观察'}))
  await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  expect(queryCameraDevices(initialWorkspace.session.session_id,'camera').devices[0]).toMatchObject({track_live:true,frame_confirmed:true})
  paused=true
  for(let i=0;i<6;i++){frames+=10;time++;await act(async()=>{await vi.advanceTimersByTimeAsync(1000)})}
  await act(async()=>{await vi.advanceTimersByTimeAsync(2500)})
  expect(stops[0]).toHaveBeenCalledTimes(1)
  expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
  expect(queryCameraDevices(initialWorkspace.session.session_id,'camera').devices[0]).toMatchObject({track_live:true,frame_confirmed:true})
 })
 it('UI pause keeps the resumed observation closed rather than automatically reopening',async()=>{
  const view=mount();await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  fireEvent.click(view.getByRole('button',{name:'恢复本地观察'}))
  await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  fireEvent.click(view.getByRole('button',{name:'暂停本地观察'}))
  await act(async()=>{await vi.advanceTimersByTimeAsync(9000)})
  expect(localApi.pauseObservation).toHaveBeenCalledTimes(1)
  expect(stops[0]).toHaveBeenCalledTimes(1)
  expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
  expect(queryCameraDevices(initialWorkspace.session.session_id,'camera').devices[0]).toMatchObject({track_live:false,frame_confirmed:false})
 })
 it('UI pause cancels an in-flight UI resume before its late API response can attach',async()=>{
  let finish!: (v:typeof initialWorkspace.session)=>void
  vi.mocked(localApi.resumeObservation).mockReturnValueOnce(new Promise(resolve=>{finish=resolve}))
  const view=mount();await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  fireEvent.click(view.getByRole('button',{name:'恢复本地观察'}))
  await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  fireEvent.click(view.getByRole('button',{name:'暂停本地观察'}))
  await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  expect(localApi.pauseObservation).toHaveBeenCalledTimes(1)
  await act(async()=>{finish({...initialWorkspace.session,observation_status:'enabled'});await vi.advanceTimersByTimeAsync(9000)})
  expect(localApi.resumeObservation).toHaveBeenCalledTimes(1)
  expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled()
  expect(localApi.sessionWorkspace).not.toHaveBeenCalled()
 })
 it('UI resume respects a privacy cancellation while its API response is pending',async()=>{
  let finish!: (v:typeof initialWorkspace.session)=>void
  vi.mocked(localApi.resumeObservation).mockReturnValueOnce(new Promise(resolve=>{finish=resolve}))
  const view=mount();await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  fireEvent.click(view.getByRole('button',{name:'恢复本地观察'}))
  await act(async()=>{await vi.advanceTimersByTimeAsync(100);window.dispatchEvent(new CustomEvent('hsc-privacy-pause'))})
  await act(async()=>{finish({...initialWorkspace.session,observation_status:'enabled'});await vi.advanceTimersByTimeAsync(9000)})
  expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled()
  expect(localApi.sessionWorkspace).not.toHaveBeenCalled()
 })

 it('a repeated legacy resume event remains resume while the UI command is still starting',async()=>{
  let finish!: (v:typeof initialWorkspace.session)=>void
  vi.mocked(localApi.resumeObservation).mockReturnValueOnce(new Promise(resolve=>{finish=resolve}))
  const view=mount();await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  fireEvent.click(view.getByRole('button',{name:'恢复本地观察'}))
  await act(async()=>{await vi.advanceTimersByTimeAsync(100);window.dispatchEvent(new CustomEvent('hsc-camera-observation-resume'))})
  await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  expect(localApi.resumeObservation).toHaveBeenCalledTimes(2)
  expect(localApi.pauseObservation).not.toHaveBeenCalled()
  expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
  await act(async()=>{finish({...initialWorkspace.session,observation_status:'enabled'});await vi.advanceTimersByTimeAsync(100)})
  expect(stops[0]).not.toHaveBeenCalled()
  expect(queryCameraDevices(initialWorkspace.session.session_id,'camera').devices[0]).toMatchObject({track_live:true,frame_confirmed:true})
 })

 it('a button still rendered as pause cannot resume in the synchronous privacy-release gap',async()=>{
  const view=mount();await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  fireEvent.click(view.getByRole('button',{name:'恢复本地观察'}))
  await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  act(()=>{
   window.dispatchEvent(new CustomEvent('hsc-privacy-pause'))
   fireEvent.click(view.getByRole('button',{name:'暂停本地观察'}))
  })
  await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  expect(localApi.resumeObservation).toHaveBeenCalledTimes(1)
  expect(localApi.pauseObservation).toHaveBeenCalledTimes(1)
  expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
  expect(stops[0]).toHaveBeenCalledTimes(1)
 })

 it.each(['muted','enabled'] as const)('retires a live %s track despite new counters, waits for lease and coverage ACKs, then reconnects',async flag=>{
  render(<SessionWorkspace initial={{...initialWorkspace,session:{...initialWorkspace.session,observation_status:'enabled'}}} childName="小辰" childGrade="初三" onBack={vi.fn()}/> )
  await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  expect(queryCameraDevices(initialWorkspace.session.session_id,'camera').devices[0].frame_confirmed).toBe(true)
  let releaseLease!:(v:Awaited<ReturnType<typeof localApi.releaseDeviceLease>>)=>void
  let releaseCoverage!:()=>void
  vi.mocked(localApi.releaseDeviceLease).mockReturnValueOnce(new Promise(resolve=>{releaseLease=resolve}))
  vi.mocked(localApi.endObservationStream).mockImplementationOnce(async (_s,epoch)=>{await new Promise<void>(resolve=>{releaseCoverage=resolve});return receipt(epoch,'stop')})
  tracks[0][flag]=flag==='muted'
  await act(async()=>{await vi.advanceTimersByTimeAsync(6100)})
  expect(stops[0]).toHaveBeenCalledTimes(1)
  expect(localApi.endObservationStream).toHaveBeenCalledTimes(1)
  expect(localApi.releaseDeviceLease).toHaveBeenCalledTimes(1)
  await act(async()=>{releaseLease({lease_id:'camera-lease',status:'released'});await vi.advanceTimersByTimeAsync(250)})
  expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
  await act(async()=>{releaseCoverage()})
  await act(async()=>{await vi.advanceTimersByTimeAsync(2500)})
  expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
  expect(queryCameraDevices(initialWorkspace.session.session_id,'camera').devices[0]).toMatchObject({track_live:true,frame_confirmed:true})
 })
 it('allows a short mute to recover on the same stream without reconnect',async()=>{
  render(<SessionWorkspace initial={{...initialWorkspace,session:{...initialWorkspace.session,observation_status:'enabled'}}} childName="小辰" childGrade="初三" onBack={vi.fn()}/> )
  await act(async()=>{await vi.advanceTimersByTimeAsync(100)})
  tracks[0].muted=true
  await act(async()=>{await vi.advanceTimersByTimeAsync(2000)})
  expect(queryCameraDevices(initialWorkspace.session.session_id,'camera').devices[0].frame_confirmed).toBe(false)
  tracks[0].muted=false
  await act(async()=>{await vi.advanceTimersByTimeAsync(4000)})
  expect(queryCameraDevices(initialWorkspace.session.session_id,'camera').devices[0].frame_confirmed).toBe(true)
  expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
  expect(stops[0]).not.toHaveBeenCalled()
 })

})
