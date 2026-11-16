import { afterEach,expect,it,vi } from 'vitest'
import { act,cleanup,fireEvent,render,screen,waitFor } from '@testing-library/react'
import { TextbookLanguageCandidateReview } from './TextbookLanguageCandidateReview'
import { requestParentDraftNavigation } from './parentDraftNavigation'
afterEach(()=>cleanup())
import { ParentTextbookDraftHost } from './parentTextbookDraftHost'
import { changeParentDraftAuthority } from './parentDraftAuthority'
import type { TextbookEditingPayload } from './parentTextbookDraftJournal'
import type { ParentDraftRecoveryFactory,ScopedParentDraftRecoveryPort } from './parentDraftRecoveryPort'
import type { ParentDraftRecoveryDraftReceipt,ParentDraftRecoverySnapshot } from '../../shared/parentDraftRecovery'
const payload=(title:string):TextbookEditingPayload=>({page:{title},capture_ref:'',capture_version:'',review_drafts:{},action_ids:{}})
const initial=(child='a'):ParentDraftRecoveryDraftReceipt=>({child_id:child,scope:'capture',storage_generation:'generation-a',revision:0,payload:null,last_action_id:null,last_expected_revision:null,updated_at:null,stale_refs:[],cloud_transmission_allowed:false})
/** Injected persistence fixture shared by fresh Hosts, not Electron/disk evidence. */
function nativeFixture(){
 const stored=new Map<string,ParentDraftRecoverySnapshot>(),revisions=new Map<string,number>(),leases=new Map<string,{key:string;sequence:number;blocked:boolean}>();let serial=0,fail=false
 let onCheckpoint:((snapshot:ParentDraftRecoverySnapshot)=>void)|undefined
 const factory:ParentDraftRecoveryFactory=(childId,scope)=>{
  const key=JSON.stringify([childId,scope]),prepared=(blocked:boolean)=>{const leaseId=`lease-${++serial}`;leases.set(leaseId,{key,sequence:0,blocked});return{leaseId,localRevision:revisions.get(key)??0,hasRecovery:blocked,protection:'native_encrypted'as const}}
  const port:ScopedParentDraftRecoveryPort={
   prepare:async()=>prepared(stored.has(key)&&!!(stored.get(key)!.pending||JSON.stringify(stored.get(key)!.latest)!==JSON.stringify(stored.get(key)!.acknowledged?.payload??null))),
   read:async(revision)=>{expect(revision).toBe(revisions.get(key));return{...prepared(false),snapshot:structuredClone(stored.get(key)!) }},
   checkpoint:(leaseId,sequence,snapshot)=>{if(fail)throw Error('synthetic disk failure');const lease=leases.get(leaseId);if(!lease||lease.blocked||lease.key!==key||sequence<=lease.sequence)throw Error('stale lease');lease.sequence=sequence;stored.set(key,structuredClone(snapshot));revisions.set(key,(revisions.get(key)??0)+1);onCheckpoint?.(snapshot);return{stored:true,sequence,localRevision:revisions.get(key)!}},
   revoke:()=>{leases.clear()},discard:async(revision,explicit)=>{expect(explicit).toBe(true);expect(revision).toBe(revisions.get(key));stored.delete(key);return prepared(false)}
  };return port
 }
 return{factory,snapshot:(child='a')=>stored.get(JSON.stringify([child,'capture'])),setFail:(v:boolean)=>{fail=v},onCheckpoint:(f:typeof onCheckpoint)=>{onCheckpoint=f}}
}
const host=(native:ReturnType<typeof nativeFixture>)=>new ParentTextbookDraftHost(native.factory)
it('retains offline exact latest and original outbox across a fresh Host without automatic PUT',async()=>{
 const disk=nativeFixture(),saved=initial(),api=vi.fn(async(_path:string,init?:RequestInit)=>{if(init?.method==='PUT')throw Error('offline');return{draft:saved}})
 const first=host(disk),entry=first.entry(api,'a','capture');await entry.journal.load();entry.journal.update(payload('未确认🙂𝒜原文'));await expect(entry.journal.flush()).rejects.toThrow()
 expect(disk.snapshot()?.latest).toEqual(payload('未确认🙂𝒜原文'));const original=structuredClone(disk.snapshot()!.pending)
 entry.journal.suspend();const nextApi=vi.fn((path:string,init?:RequestInit)=>api(path,init)),next=host(disk).entry(nextApi,'a','capture');await expect(next.journal.load()).rejects.toThrow();expect(next.journal.state.payload).toBeNull()
 await next.journal.reconcileAuthority();expect(next.journal.state.payload).toEqual(payload('未确认🙂𝒜原文'));expect(disk.snapshot()!.pending).toEqual(original);expect(nextApi.mock.calls.every(([,init])=>!init?.method||init.method==='GET')).toBe(true)
})
it('restores committed lost-reply pending and newer raw, clears only that pending, and never auto PUTs',async()=>{
 const disk=nativeFixture();let saved=initial(),finish!:(v:unknown)=>void
 const api=vi.fn(async(_path:string,init?:RequestInit)=>{if(init?.method==='PUT'){const request=JSON.parse(init.body as string);saved={...saved,revision:1,payload:request.payload,last_action_id:request.client_action_id,last_expected_revision:0};return await new Promise(resolve=>{finish=resolve})}return{draft:saved}})
 const one=host(disk).entry(api,'a','capture');await one.journal.load();one.journal.update(payload('sent original'));await vi.waitFor(()=>expect(finish).toBeTypeOf('function'));const old=structuredClone(disk.snapshot()!.pending)
 one.journal.keepEditing(payload('newer private raw🙂'));one.journal.suspend();expect(disk.snapshot()!.pending).toEqual(old)
 const restoredApi=vi.fn(async()=>({draft:saved})),two=host(disk).entry(restoredApi,'a','capture');await expect(two.journal.load()).rejects.toThrow();await two.journal.reconcileAuthority()
 expect(two.journal.state.payload).toEqual(payload('newer private raw🙂'));expect(disk.snapshot()!.latest).toEqual(payload('newer private raw🙂'));expect(disk.snapshot()!.pending).toBeNull();expect(restoredApi.mock.calls.length).toBeGreaterThan(0)
 finish({draft:saved});await Promise.resolve();expect(disk.snapshot()!.latest).toEqual(payload('newer private raw🙂'));expect(api.mock.calls.filter(([,init])=>init?.method==='PUT')).toHaveLength(1)
})
it('does not send PUT when synchronous durable checkpoint fails and preserves private RAM',async()=>{
 const disk=nativeFixture(),api=vi.fn(async()=>({draft:initial()})),entry=host(disk).entry(api,'a','capture');await entry.journal.load();disk.setFail(true);entry.journal.update(payload('must not disappear'))
 await expect(entry.journal.flush()).rejects.toThrow();expect(entry.journal.state.payload).toEqual(payload('must not disappear'));expect(api.mock.calls).toHaveLength(1);expect(entry.journal.hasUnconfirmedEditing(entry.journal.state.payload)).toBe(true)
})
it('rechecks authority after native pending ACK and blocks HTTP if lock happens inside checkpoint',async()=>{
 const disk=nativeFixture(),api=vi.fn(async()=>({draft:initial()})),owner=host(disk),detach=owner.attach(),entry=owner.entry(api,'a','capture');await entry.journal.load();let fired=false
 disk.onCheckpoint(snapshot=>{if(snapshot.pending&&!fired){fired=true;changeParentDraftAuthority('locked')}})
 try{entry.journal.update(payload('lock before network'));await expect(entry.journal.flush()).rejects.toThrow();expect(fired).toBe(true);expect(api.mock.calls).toHaveLength(1);expect(disk.snapshot()!.latest).toEqual(payload('lock before network'))}finally{detach()}
})
it('keeps restored old-generation pending in conflict and isolates another child',async()=>{
 const disk=nativeFixture(),api=vi.fn(async(_path:string,init?:RequestInit)=>{if(init?.method==='PUT')throw Error('offline');return{draft:initial()}}),one=host(disk).entry(api,'a','capture');await one.journal.load();one.journal.update(payload('a-only'));await expect(one.journal.flush()).rejects.toThrow();one.journal.suspend()
 const nextApi=vi.fn(async()=>({draft:{...initial(),storage_generation:'generation-restored'}})),two=host(disk).entry(nextApi,'a','capture');await expect(two.journal.load()).rejects.toThrow();await two.journal.reconcileAuthority();expect(two.journal.state.phase).toBe('conflict');expect(disk.snapshot()!.pending?.expected_generation).toBe('generation-a')
 const other=host(disk).entry(async()=>({draft:initial('b')}),'b','capture');await other.journal.load();expect(other.journal.state.payload).toBeNull();expect(disk.snapshot('b')?.latest??null).toBeNull()
})

it('protects acknowledged-null raw without replacing it on the first explicit server read',async()=>{
 const disk=nativeFixture(),port=disk.factory('a','capture') as ScopedParentDraftRecoveryPort,grant=await port.prepare()
 port.checkpoint(grant.leaseId,1,{schema:1,childId:'a',scope:'capture',acknowledged:null,latest:payload('first offline original🙂'),pending:null});port.revoke()
 const api=vi.fn(async()=>({draft:initial()})),entry=host(disk).entry(api,'a','capture');await expect(entry.journal.load()).rejects.toThrow();expect(api.mock.calls).toHaveLength(0)
 await entry.journal.reconcileAuthority();expect(entry.journal.state.payload).toEqual(payload('first offline original🙂'));expect(entry.journal.state.phase).toBe('rejected');expect(disk.snapshot()!.latest).toEqual(payload('first offline original🙂'))
})
it('rejects a late native prepare grant after authority changes and performs no draft HTTP',async()=>{
 const disk=nativeFixture();let finish!:(v:unknown)=>void
 const prepare=vi.fn(()=>new Promise(resolve=>{finish=resolve})),factory:ParentDraftRecoveryFactory=(child,scope)=>({...disk.factory(child,scope) as ScopedParentDraftRecoveryPort,prepare:prepare as ScopedParentDraftRecoveryPort['prepare']})
 const owner=new ParentTextbookDraftHost(factory),detach=owner.attach(),api=vi.fn(async()=>({draft:initial()})),entry=owner.entry(api,'a','capture'),loading=entry.journal.load()
 changeParentDraftAuthority('locked');finish({leaseId:'old',localRevision:0,hasRecovery:false,protection:'native_encrypted'})
 try{await expect(loading).rejects.toThrow();expect(api.mock.calls).toHaveLength(0);expect(entry.journal.authoritySuspended).toBe(true)}finally{detach()}
})

it('uses the real editor to recover offline last input in a fresh Host only after an explicit read',async()=>{
 const disk=nativeFixture(),api=vi.fn(async(path:string,init?:RequestInit)=>{if(init?.method==='PUT')throw Error('offline');return path.includes('/drafts/')?{draft:initial()}:{items:[],nodes:[]}})
 const one=host(disk),detach=one.attach(),first=render(<TextbookLanguageCandidateReview draftHost={one} api={api} childId="a" captureOnly />)
 await waitFor(()=>expect(screen.getByLabelText('教材页标题')).not.toBeDisabled())
 fireEvent.change(screen.getByLabelText('教材页标题'),{target:{value:'fresh-process retained🙂𝒜'}})
 expect(disk.snapshot()!.latest?.page.title).toBe('fresh-process retained🙂𝒜')
 expect(screen.getByText(/加密保护在这台电脑/)).toBeInTheDocument()
 first.unmount();detach()
 const restoredApi=vi.fn(async(path:string,init?:RequestInit)=>{if(init?.method==='PUT')throw Error('must not auto write');return path.includes('/drafts/')?{draft:initial()}:{items:[],nodes:[]}}),two=host(disk),nextDetach=two.attach()
 try{
  render(<TextbookLanguageCandidateReview draftHost={two} api={restoredApi} childId="a" captureOnly />)
  await screen.findByRole('button',{name:'核对并恢复本窗口草稿'});expect(screen.queryByDisplayValue('fresh-process retained🙂𝒜')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button',{name:'核对并恢复本窗口草稿'}))
  await waitFor(()=>expect(screen.getByLabelText('教材页标题')).toHaveValue('fresh-process retained🙂𝒜'))
  expect(restoredApi.mock.calls.filter(([,init])=>init?.method==='PUT')).toHaveLength(0)
 }finally{cleanup();nextDetach()}
})
it('shows protection unconfirmed and leaves editing disabled when the product has no native port',async()=>{
 const api=vi.fn(async()=>({items:[],nodes:[]})),owner=new ParentTextbookDraftHost()
 render(<TextbookLanguageCandidateReview draftHost={owner} api={api} childId="a" captureOnly />)
 await screen.findByText(/尚未确认这台电脑的加密草稿保护/)
 expect(screen.getByLabelText('教材页标题')).toBeDisabled()
})
it('discards late native raw after lock before hydrating or reading the backend',async()=>{
 const disk=nativeFixture(),seed=disk.factory('a','capture')as ScopedParentDraftRecoveryPort,grant=await seed.prepare();seed.checkpoint(grant.leaseId,1,{schema:1,childId:'a',scope:'capture',acknowledged:initial(),latest:payload('hidden original'),pending:null});seed.revoke()
 let finish!:(v:Awaited<ReturnType<ScopedParentDraftRecoveryPort['read']>>)=>void,requested=false
 const factory:ParentDraftRecoveryFactory=(child,scope)=>({...disk.factory(child,scope)as ScopedParentDraftRecoveryPort,read:()=>{requested=true;return new Promise(resolve=>{finish=resolve})as ReturnType<ScopedParentDraftRecoveryPort['read']>}})
 const owner=new ParentTextbookDraftHost(factory),detach=owner.attach(),api=vi.fn(async()=>({draft:initial()})),entry=owner.entry(api,'a','capture');await expect(entry.journal.load()).rejects.toThrow();const recovery=entry.journal.reconcileAuthority();await vi.waitFor(()=>expect(requested).toBe(true));changeParentDraftAuthority('locked');finish({...grant,snapshot:disk.snapshot()!})
 try{await expect(recovery).rejects.toThrow();expect(entry.journal.state.payload).toBeNull();expect(api.mock.calls).toHaveLength(0);expect(disk.snapshot()!.latest).toEqual(payload('hidden original'))}finally{detach()}
})
it('rejects recovered other-child snapshots before any original GET or state adoption',async()=>{
 const disk=nativeFixture(),seed=disk.factory('a','capture')as ScopedParentDraftRecoveryPort,grant=await seed.prepare();seed.checkpoint(grant.leaseId,1,{schema:1,childId:'a',scope:'capture',acknowledged:initial(),latest:payload('a-only'),pending:null});seed.revoke()
 const factory:ParentDraftRecoveryFactory=(child,scope)=>{const original=disk.factory(child,scope)as ScopedParentDraftRecoveryPort;return{...original,read:async revision=>{const r=await original.read(revision);return{...r,snapshot:{...r.snapshot,childId:'b'}}}}}
 const api=vi.fn(async()=>({draft:initial()})),entry=new ParentTextbookDraftHost(factory).entry(api,'a','capture');await expect(entry.journal.load()).rejects.toThrow();await expect(entry.journal.reconcileAuthority()).rejects.toThrow();expect(api.mock.calls).toHaveLength(0);expect(entry.journal.state.payload).toBeNull();expect(disk.snapshot()!.latest).toEqual(payload('a-only'))
})
it('allows ordinary leave only after the exact dirty input has a native receipt, not memory-only protection',async()=>{
 const disk=nativeFixture(),api=vi.fn(async(_path:string,init?:RequestInit)=>{if(init?.method==='PUT')throw Error('offline');return{draft:initial()}}),entry=host(disk).entry(api,'a','capture');await entry.journal.load();entry.journal.update(payload('protected offline'));await expect(entry.journal.flush()).rejects.toThrow();expect(entry.journal.hasUnconfirmedEditing(entry.journal.state.payload)).toBe(false);entry.journal.suspend();expect(entry.journal.hasUnconfirmedEditing(entry.journal.state.payload)).toBe(false)
 disk.setFail(true);entry.journal.keepEditing(payload('not protected after lock'));expect(entry.journal.hasUnconfirmedEditing(entry.journal.state.payload)).toBe(true)
})

it('flushes every scope before the first sender-wide native revoke',async()=>{
 const disk=nativeFixture(),events:string[]=[],factory:ParentDraftRecoveryFactory=(child,scope)=>{const p=disk.factory(child,scope)as ScopedParentDraftRecoveryPort;return{...p,checkpoint:(...args)=>{events.push('write:'+child);return p.checkpoint(...args)},revoke:()=>{events.push('revoke:'+child);p.revoke()}}},owner=new ParentTextbookDraftHost(factory)
 const a=owner.entry(async()=>({draft:initial('a')}),'a','capture'),b=owner.entry(async()=>({draft:initial('b')}),'b','capture');await a.journal.load();await b.journal.load();events.length=0;a.capture=()=>payload('a last');b.capture=()=>payload('b last');owner.suspendAll()
 expect(events.indexOf('revoke:a')).toBeGreaterThan(events.lastIndexOf('write:b'));expect(disk.snapshot('a')!.latest).toEqual(payload('a last'));expect(disk.snapshot('b')!.latest).toEqual(payload('b last'))
})
it('rejects a stale native ACK without sending HTTP or claiming protection',async()=>{
 const disk=nativeFixture();let bad=false
 const factory:ParentDraftRecoveryFactory=(child,scope)=>{const p=disk.factory(child,scope)as ScopedParentDraftRecoveryPort;return{...p,checkpoint:(...args)=>{const ack=p.checkpoint(...args);return bad?{...ack,sequence:0}:ack}}},api=vi.fn(async()=>({draft:initial()})),entry=new ParentTextbookDraftHost(factory).entry(api,'a','capture');await entry.journal.load();bad=true;entry.journal.update(payload('retained after stale ACK'));await expect(entry.journal.flush()).rejects.toThrow();expect(api.mock.calls).toHaveLength(1);expect(entry.journal.state.payload).toEqual(payload('retained after stale ACK'));expect(entry.journal.hasUnconfirmedEditing(entry.journal.state.payload)).toBe(true)
})
it('keeps visible last input and blocks ordinary navigation when native storage fails',async()=>{
 const disk=nativeFixture(),api=vi.fn(async(path:string,init?:RequestInit)=>{if(init?.method==='PUT')throw Error('offline');return path.includes('/drafts/')?{draft:initial()}:{items:[],nodes:[]}}),owner=host(disk),detach=owner.attach()
 try{render(<TextbookLanguageCandidateReview draftHost={owner} api={api} childId="a" captureOnly/>);await waitFor(()=>expect(screen.getByLabelText('教材页标题')).not.toBeDisabled());disk.setFail(true);fireEvent.change(screen.getByLabelText('教材页标题'),{target:{value:'unprotected original🙂'}});expect(screen.getByLabelText('教材页标题')).toHaveValue('unprotected original🙂');expect(screen.getByText(/尚未确认这台电脑的加密保护/)).toBeInTheDocument();let allowed=true;act(()=>{allowed=requestParentDraftNavigation()});expect(allowed).toBe(false)}finally{cleanup();detach()}
})
it('requires explicit discard and reads the saved draft without an automatic PUT',async()=>{
 const disk=nativeFixture(),p=disk.factory('a','capture')as ScopedParentDraftRecoveryPort,g=await p.prepare();p.checkpoint(g.leaseId,1,{schema:1,childId:'a',scope:'capture',acknowledged:initial(),latest:payload('explicitly discarded'),pending:null});p.revoke()
 const api=vi.fn(async(path:string,_init?:RequestInit)=>path.includes('/drafts/')?{draft:initial()}:{items:[],nodes:[]}),owner=host(disk),detach=owner.attach()
 try{render(<TextbookLanguageCandidateReview draftHost={owner} api={api} childId="a" captureOnly/>);const button=await screen.findByRole('button',{name:'放弃本机恢复草稿并读取已保存版本'});expect(button).toBeDisabled();fireEvent.click(screen.getByRole('checkbox',{name:'我明确放弃这台电脑尚未确认的恢复草稿'}));fireEvent.click(button);await waitFor(()=>expect(screen.getByLabelText('教材页标题')).not.toBeDisabled());expect(api.mock.calls.every(([,init])=>!init?.method||init.method==='GET')).toBe(true);expect(disk.snapshot()!.latest).toBeNull()}finally{cleanup();detach()}
})

it('retains the exact native protection ACK through duplicate Host suspension without reviving authority',async()=>{
 const disk=nativeFixture(),api=vi.fn(async()=>({draft:initial()})),owner=host(disk),detach=owner.attach(),entry=owner.entry(api,'a','capture')
 try{
  await entry.journal.load();const raw=payload('public synthetic protected draft');entry.capture=()=>raw;entry.journal.keepEditing(raw)
  const protectedSnapshot=structuredClone(disk.snapshot()!)
  expect(entry.journal.hasUnconfirmedEditing(raw)).toBe(false)
  owner.suspendAll()
  expect(entry.journal.hasUnconfirmedEditing(raw)).toBe(false)
  const calls=api.mock.calls.length
  owner.suspendAll()
  expect(disk.snapshot()).toEqual(protectedSnapshot)
  expect(entry.journal.hasUnconfirmedEditing(raw)).toBe(false)
  const unload=new Event('beforeunload',{cancelable:true});window.dispatchEvent(unload);expect(unload.defaultPrevented).toBe(false)
  await expect(entry.journal.requestActive('/forbidden-after-revoke')).rejects.toThrow()
  expect(api.mock.calls).toHaveLength(calls)
 }finally{detach()}
})
it('does not reuse an earlier protection ACK for changed editing after native revoke',async()=>{
 const disk=nativeFixture(),api=vi.fn(async()=>({draft:initial()})),owner=host(disk),detach=owner.attach(),entry=owner.entry(api,'a','capture')
 try{
  await entry.journal.load();let raw=payload('original protected public draft');entry.capture=()=>raw;entry.journal.keepEditing(raw);owner.suspendAll()
  const protectedSnapshot=structuredClone(disk.snapshot()!),calls=api.mock.calls.length
  raw=payload('changed after native revoke');owner.suspendAll()
  expect(disk.snapshot()).toEqual(protectedSnapshot)
  expect(entry.journal.state.payload).toEqual(raw)
  expect(entry.journal.hasUnconfirmedEditing(raw)).toBe(true)
  const unload=new Event('beforeunload',{cancelable:true});window.dispatchEvent(unload);expect(unload.defaultPrevented).toBe(true)
  await expect(entry.journal.flush()).rejects.toThrow()
  expect(api.mock.calls).toHaveLength(calls)
 }finally{detach()}
})
