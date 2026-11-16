import {act,cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import type {HouseholdNativeEntry} from './types'
const h=vi.hoisted(()=>({wake:null as null|((profile:{childId:string;wakeWord:string;configured:boolean},command:string,entry?:HouseholdNativeEntry)=>Promise<void>),entry:undefined as HouseholdNativeEntry|undefined,bootstrap:vi.fn(),installationSetupStatus:vi.fn(async()=>null),startSession:vi.fn(),sessionWorkspace:vi.fn()}))
vi.mock('./api',()=>({createParentDraftRecoveryPort:()=>undefined,localApi:h,resetRuntimeCache:vi.fn(),ApiError:class extends Error{}}))
vi.mock('./LocalInputs',()=>({HouseholdStandbyVoiceInput:(props:{onWake:typeof h.wake})=>{h.wake=props.onWake;return null}}))
vi.mock('./SessionWorkspace',()=>({SessionWorkspace:(props:{initialHouseholdEntry?:HouseholdNativeEntry})=>{h.entry=props.initialHouseholdEntry;return <div>学习工作区</div>}}))
vi.mock('./ParentCenter',()=>({ParentCenter:()=> <div>家长工作区</div>}))
vi.mock('./DeviceAvailabilityStrip',()=>({DeviceAvailabilityStrip:()=>null}))
import {App} from './App'
const child={child_id:'public-child',nickname:'合成同学',grade:'四年级',status:'active'}
const entry:HouseholdNativeEntry={entry_receipt:{version:1,origin_scope:'unassigned_household',storage_generation:'public-storage',receipt_id:'original-receipt',client_action_id:'original-local-asr',issued_at_ms:12345,transcript_sha256:'a'.repeat(64),model_version:'local-asr',source_kind:'household_local_transcript'},transcript:'小知，帮我打开错题本'}
beforeEach(()=>{localStorage.clear();vi.clearAllMocks();h.entry=undefined;h.bootstrap.mockResolvedValue({children:[child],active_session:null,devices:[],model:{configured:false},privacy_paused:false});h.startSession.mockResolvedValue({session_id:'new-session',child_id:child.child_id});h.sessionWorkspace.mockResolvedValue({session:{session_id:'new-session',child_id:child.child_id}});vi.stubGlobal('desktop',{getWakeWordSettings:async()=>({childId:child.child_id,wakeWord:'小知',enabled:true})})})
afterEach(()=>{cleanup();vi.unstubAllGlobals()})
it('carries the untouched full transcript and receipt through actual Home selection instead of relabelling the stripped command as cloud ASR',async()=>{
 render(<App/>);await screen.findByText(child.nickname);await waitFor(()=>expect(h.wake).toBeTruthy())
 await act(async()=>{await h.wake!({childId:child.child_id,wakeWord:'小知',configured:true},'帮我打开错题本',entry)})
 expect(await screen.findByText('学习工作区')).toBeVisible();expect(h.entry).toEqual(entry);expect(h.entry!.transcript).toMatch(/^小知/)
 expect(h.startSession).toHaveBeenCalledExactlyOnceWith(child.child_id)
})
it('a queued old Home ASR result with a genuine receipt cannot enter another view after parent selection',async()=>{
 render(<App/>);await screen.findByText(child.nickname);await waitFor(()=>expect(h.wake).toBeTruthy());const old=h.wake!
 fireEvent.click(screen.getByRole('button',{name:/家长中心/}));await act(async()=>{await old({childId:child.child_id,wakeWord:'小知',configured:true},'帮我打开错题本',entry)})
 expect(screen.getByText('家长工作区')).toBeVisible();expect(h.startSession).not.toHaveBeenCalled();expect(h.sessionWorkspace).not.toHaveBeenCalled();expect(h.entry).toBeUndefined()
})
it('an already-started Home session read cannot deliver its receipt after another explicit view owns the result',async()=>{
 let resolve!:(value:unknown)=>void;h.sessionWorkspace.mockImplementation(()=>new Promise(done=>{resolve=done}))
 render(<App/>);await screen.findByText(child.nickname);await waitFor(()=>expect(h.wake).toBeTruthy())
 let pending!:Promise<void>;await act(async()=>{pending=h.wake!({childId:child.child_id,wakeWord:'小知',configured:true},'帮我打开错题本',entry);await Promise.resolve()})
 await waitFor(()=>expect(h.sessionWorkspace).toHaveBeenCalledTimes(1));fireEvent.click(screen.getByRole('button',{name:/家长中心/}))
 await act(async()=>{resolve({session:{session_id:'new-session',child_id:child.child_id}});await pending})
 expect(screen.getByText('家长工作区')).toBeVisible();expect(h.entry).toBeUndefined()
})
