import { expect,it } from 'vitest'
import { ParentTextbookDraftJournal,type TextbookEditingPayload } from './parentTextbookDraftJournal'
const value=(title:string):TextbookEditingPayload=>({page:{title},capture_ref:'',capture_version:'',review_drafts:{},action_ids:{}})
const fresh=()=>({child_id:'a',scope:'capture',storage_generation:'generation-a',revision:0,payload:null as TextbookEditingPayload|null,last_action_id:null as string|null,last_expected_revision:null as number|null,updated_at:null,stale_refs:[],cloud_transmission_allowed:false as const})
it('suspends a committed lost reply without readback, reconciles explicitly, and never autosends newer editing',async()=>{
 let d=fresh(),release!:()=>void,reads=0;const puts:unknown[]=[]
 const api=async(_p:string,init?:RequestInit)=>{
  if(!init){reads++;return{draft:structuredClone(d)}}
  const r=JSON.parse(String(init.body));puts.push(r);d={...d,revision:r.expected_revision+1,payload:r.payload,last_action_id:r.client_action_id,last_expected_revision:r.expected_revision}
  await new Promise<void>(r=>{release=r});throw Error('lost reply')
 }
 const j=new ParentTextbookDraftJournal(api,'a','capture',undefined,'memory-only');await j.load();j.update(value('sent'))
 await Promise.resolve();expect(puts).toHaveLength(1)
 j.keepEditing(value('newer raw'));j.suspend();release();await Promise.resolve();await Promise.resolve()
 expect(reads).toBe(1);expect(j.state.payload).toEqual(value('newer raw'))
 await j.reconcileAuthority();expect(reads).toBe(2);expect(puts).toHaveLength(1)
 expect(j.state.payload).toEqual(value('newer raw'));expect(j.state.phase).toBe('rejected')
})
it('does not consume a late authority GET after another lock, and rejects an old generation without writing',async()=>{
 let d=fresh(),hold=false,release!:()=>void,puts=0
 const api=async(_p:string,init?:RequestInit)=>{if(init){puts++;throw Error('offline')};const read=structuredClone(d);if(hold)await new Promise<void>(r=>{release=r});return{draft:read}}
 const j=new ParentTextbookDraftJournal(api,'a','capture',undefined,'memory-only');await j.load();j.keepEditing(value('private'));j.suspend();hold=true
 const first=j.reconcileAuthority();await Promise.resolve();j.suspend();release();await expect(first).rejects.toThrow()
 expect(j.authoritySuspended).toBe(true);expect(j.state.payload).toEqual(value('private'))
 hold=false;d={...d,storage_generation:'generation-b'};await j.reconcileAuthority()
 expect(j.state.phase).toBe('conflict');expect(j.state.payload).toEqual(value('private'));expect(puts).toBe(0)
 const both=await j.inspectConflict();expect(both.generationChanged).toBe(true);expect(both.local).toEqual(value('private'))
})
it('keeps an uncommitted pending action identical through lock, explicit reconciliation, and retry',async()=>{
 let d=fresh(),offline=true;const actions:string[]=[]
 const api=async(_p:string,init?:RequestInit)=>{if(!init)return{draft:structuredClone(d)};const r=JSON.parse(String(init.body));actions.push(r.client_action_id);if(offline)throw Error('offline');d={...d,revision:r.expected_revision+1,payload:r.payload,last_action_id:r.client_action_id,last_expected_revision:r.expected_revision};return{draft:structuredClone(d)}}
 const j=new ParentTextbookDraftJournal(api,'a','capture',undefined,'memory-only');await j.load();j.update(value('original'));await expect(j.flush()).rejects.toThrow();j.suspend()
 offline=false;await j.reconcileAuthority();expect(actions).toHaveLength(1);await j.retry();expect(actions).toHaveLength(2);expect(actions[1]).toBe(actions[0]);expect(j.state.phase).toBe('saved')
})
it.each([1,2,3,4,5,6,7])('retains the original pending and acknowledgement when lock reaches the outer await boundary at microtask depth %i',async(depth)=>{
 type Internals={pending:unknown;acknowledged:{revision:number}}
 let d=fresh(),j:ParentTextbookDraftJournal,arm=false,atLock:{pending:unknown;revision:number}|null=null
 const api=async(_p:string,init?:RequestInit)=>{
  if(!init)return{draft:structuredClone(d)}
  const request=JSON.parse(String(init.body));d={...d,revision:request.expected_revision+1,payload:request.payload,last_action_id:request.client_action_id,last_expected_revision:request.expected_revision}
  if(arm){const lock=()=>{j.suspend();const x=j as unknown as Internals;atLock={pending:structuredClone(x.pending),revision:x.acknowledged.revision}};let schedule=lock;for(let k=0;k<depth;k++){const next=schedule;schedule=()=>queueMicrotask(next)}schedule()}
  return{draft:structuredClone(d)}
 }
 j=new ParentTextbookDraftJournal(api,'a','capture',undefined,'memory-only');await j.load();arm=true;j.update(value('public original action'))
 await new Promise(resolve=>setTimeout(resolve,0));expect(atLock).not.toBeNull();expect(j.authoritySuspended).toBe(true)
 const locked=atLock as unknown as {pending:unknown;revision:number},after=j as unknown as Internals
 if(locked.pending){expect(after.pending).toEqual(locked.pending);expect(after.acknowledged.revision).toBe(locked.revision)}
 else expect(after.acknowledged.revision).toBe(locked.revision)
})
