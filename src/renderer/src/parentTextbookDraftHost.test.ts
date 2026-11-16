import { expect,it } from 'vitest'
import { ParentTextbookDraftHost } from './parentTextbookDraftHost'
import { changeParentDraftAuthority } from './parentDraftAuthority'
const draft=(child_id:string)=>({draft:{child_id,scope:'capture',storage_generation:'generation-a',revision:0,payload:null,last_action_id:null,last_expected_revision:null,updated_at:null,stale_refs:[],cloud_transmission_allowed:false}})
it('retains A private editing across locked B entry with zero A reads or writes, and guards closing while locked',async()=>{
 const host=new ParentTextbookDraftHost(()=>'memory-only'),detach=host.attach();const calls:string[]=[]
 const api=async(path:string,init?:RequestInit)=>{calls.push((init?'PUT ':'GET ')+path);return draft(path.includes('/a/')?'a':'b')}
 try {
  const a=host.entry(api,'a','capture');await a.journal.load()
  const raw={page:{title:'A private🙂'},capture_ref:'',capture_version:'',review_drafts:{},action_ids:{}}
  a.capture=()=>raw
  changeParentDraftAuthority('locked');expect(a.journal.state.payload).toEqual(raw)
  const locked=calls.length;await expect(a.journal.requestActive('/forbidden')).rejects.toThrow();expect(calls).toHaveLength(locked)
  const unload=new Event('beforeunload',{cancelable:true});window.dispatchEvent(unload);expect(unload.defaultPrevented).toBe(true)
  changeParentDraftAuthority('unlocked')
  const b=host.entry(api,'b','capture');await b.journal.load();expect(b.journal.state.payload).toBeNull()
  expect(calls.filter(p=>p.includes('/a/'))).toHaveLength(1);expect(a.journal.state.payload).toEqual(raw)
  expect(host.entry(api,'a','capture')).toBe(a);await a.journal.reconcileAuthority()
  expect(a.journal.state.payload).toEqual(raw);expect(a.journal.state.phase).toBe('rejected')
  expect(calls.filter(p=>p.startsWith('PUT'))).toHaveLength(0)
 }finally{detach()}
})
it('an editor API captured before lock cannot regain permission when that same journal is explicitly recovered',async()=>{
 const host=new ParentTextbookDraftHost(()=>'memory-only'),detach=host.attach();let reads=0
 try{const entry=host.entry(async()=>{reads++;return draft('a')},'a','capture');await entry.journal.load();const old=entry.journal.editingApi()
 changeParentDraftAuthority('locked');changeParentDraftAuthority('unlocked');await entry.journal.reconcileAuthority()
 const before=reads;await expect(old('/old')).rejects.toThrow();expect(reads).toBe(before)
 await entry.journal.editingApi()('/new');expect(reads).toBe(before+1)
 }finally{detach()}
})
