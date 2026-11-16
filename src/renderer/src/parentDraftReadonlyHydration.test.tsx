import {afterEach,it,expect,vi} from 'vitest'
import {act,cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'
import {createCipheriv,createDecipheriv,randomBytes} from 'node:crypto'
import {TextbookLanguageCandidateReview} from './TextbookLanguageCandidateReview'
import {ParentTextbookDraftHost} from './parentTextbookDraftHost'
import {requestParentDraftNavigation} from './parentDraftNavigation'
import {ParentDraftRecoveryStore,ParentDraftRecoveryCoordinator,type ParentDraftRecoveryCodec} from '../../main/parent-draft-recovery'
import {canonical} from '../../main/parent-draft-recovery-validation'
afterEach(()=>cleanup())
const payload={page:{title:'E3 synthetic saved parent editing',ocr_text:'E3 SYNTHETIC TEST TEXT; not an OCR transcription of the public PDF.',unit:'PUBLIC unrendered original unit',source_ref:'PUBLIC retained source'},capture_ref:'',capture_version:'',review_drafts:{},action_ids:{},objective_draft:null}
function fixture(initial:any=payload){
 const root=fs.mkdtempSync(path.join(fs.realpathSync(os.tmpdir()),'readonly-hydration-')),key=randomBytes(32),profile='fixed-public-profile';
 const codec:ParentDraftRecoveryCodec={assertAvailable(){},encrypt(plain){const iv=randomBytes(12),c=createCipheriv('aes-256-gcm',key,iv),body=Buffer.concat([c.update(plain,'utf8'),c.final()]);return Buffer.concat([iv,c.getAuthTag(),body])},decrypt(raw){const d=createDecipheriv('aes-256-gcm',key,raw.subarray(0,12));d.setAuthTag(raw.subarray(12,28));return Buffer.concat([d.update(raw.subarray(28)),d.final()]).toString()}}
 const stored=new Map(['child-primary-4','child-junior-3'].map(child=>[child,structuredClone(initial)])),versions=new Map([...stored.keys()].map(child=>[child,initial===null?0:1]));const calls:{url:string;init?:RequestInit}[]=[];
 let pagePosts=0;
 const receipt=(child:string)=>({child_id:child,scope:'capture' as const,storage_generation:'fixed-public-current-generation',revision:versions.get(child)!,payload:structuredClone(stored.get(child)),last_action_id:versions.get(child)!>0?'fixed-public-'+child:null,last_expected_revision:versions.get(child)!>0?versions.get(child)!-1:null,updated_at:versions.get(child)!>0?'2026-10-24T00:00:00Z':null,stale_refs:[],cloud_transmission_allowed:false as const})
 const api=vi.fn(async(url:string,init?:RequestInit)=>{
  calls.push({url,init});const child=['child-primary-4','child-junior-3'].find(c=>url.includes('/'+c+'/drafts/capture'));
  if(child){if(init?.method==='PUT'){const body=JSON.parse(init.body as string);expect(body.expected_generation).toBe('fixed-public-current-generation');expect(body.expected_revision).toBe(versions.get(child));stored.set(child,structuredClone(body.payload));versions.set(child,versions.get(child)!+1);return{draft:{...receipt(child),last_action_id:body.client_action_id,last_expected_revision:body.expected_revision}}}return{draft:receipt(child)}}
  if(url.endsWith('/textbook-pages')&&init?.method==='POST'){pagePosts++;const body=JSON.parse(init.body as string);return{page:{...body,page_ref:'fixed-public-page'}}}
  return{items:[],nodes:[]}
 })
 const create=()=>{const store=new ParentDraftRecoveryStore({userDataPath:root,profileId:profile,codec}),coordinator=new ParentDraftRecoveryCoordinator({store,authorize:async()=>({profileId:profile})}),host=new ParentTextbookDraftHost((child,scope)=>({prepare:()=>coordinator.prepare(7,child,scope,'fixed-public-parent'),read:r=>coordinator.read(7,child,scope,'fixed-public-parent',r),checkpoint:(lease,seq,snapshot)=>coordinator.checkpoint(7,lease,seq,snapshot),revoke:()=>coordinator.revokeSender(7),discard:r=>coordinator.discard(7,child,scope,'fixed-public-parent',r,true)}));return{store,coordinator,host}}
 return{root,key,api,calls,stored,versions,receipt,create,pagePosts:()=>pagePosts,puts:()=>calls.filter(x=>x.init?.method==='PUT'),dispose(){cleanup();key.fill(0);fs.rmSync(root,{recursive:true,force:true})}}
}
it.each(['partial','null'])('loading an unchanged legal %s receipt never PUTs defaults or blocks clean navigation; real Host/disk reopen retains exact saved bytes',async(kind)=>{
 const initial=kind==='null'?null:payload,f=fixture(initial),one=f.create(),detach=one.host.attach();let nextDetach:undefined|(()=>void),firstDetached=false;
 try{
  const first=render(<TextbookLanguageCandidateReview draftHost={one.host} childId="child-primary-4" captureOnly api={f.api}/>);
  await waitFor(()=>expect(screen.getByLabelText('教材页标题')).not.toBeDisabled());
  await act(async()=>{await Promise.resolve()});expect(f.puts()).toHaveLength(0);
  expect(canonical(f.stored.get('child-primary-4'))).toBe(canonical(initial));expect(one.store.read('child-primary-4','capture').snapshot?.latest).toEqual(initial);
  const unload=new Event('beforeunload',{cancelable:true});window.dispatchEvent(unload);expect(unload.defaultPrevented).toBe(false);expect(requestParentDraftNavigation()).toBe(true);
  first.unmount();detach();firstDetached=true;expect(one.store.read('child-primary-4','capture').snapshot?.latest).toEqual(initial);
  const two=f.create();nextDetach=two.host.attach();render(<TextbookLanguageCandidateReview draftHost={two.host} childId="child-primary-4" captureOnly api={f.api}/>);await waitFor(()=>expect(screen.getByLabelText('教材页标题')).not.toBeDisabled());
  expect(f.puts()).toHaveLength(0);expect(two.store.read('child-primary-4','capture').snapshot?.acknowledged?.payload).toEqual(initial);expect(f.stored.get('child-junior-3')).toEqual(initial);
 }finally{cleanup();nextDetach?.();if(!firstDetached)detach();f.dispose()}
})
it('a genuine edit after hydration still saves and native-flushes original text and untouched page fields without crossing children',async()=>{
 const f=fixture(),one=f.create(),detach=one.host.attach();try{
  render(<TextbookLanguageCandidateReview draftHost={one.host} childId="child-primary-4" captureOnly api={f.api}/>);await waitFor(()=>expect(screen.getByLabelText('教材页标题')).not.toBeDisabled());expect(f.puts()).toHaveLength(0);
  fireEvent.change(screen.getByLabelText('教材页标题'),{target:{value:'家长真实修改🙂𝒜'}});await waitFor(()=>expect(f.puts()).toHaveLength(1));await one.host.entry(f.api,'child-primary-4','capture').journal.flush();
  expect(f.stored.get('child-primary-4').page.title).toBe('家长真实修改🙂𝒜');expect(f.stored.get('child-primary-4').page.ocr_text).toBe(payload.page.ocr_text);expect(f.stored.get('child-primary-4').page.unit).toBe(payload.page.unit);expect(f.stored.get('child-primary-4').page.source_ref).toBe(payload.page.source_ref);expect(f.stored.get('child-junior-3')).toEqual(payload);
  expect(one.store.read('child-primary-4','capture').snapshot?.latest).toEqual(f.stored.get('child-primary-4'));expect(requestParentDraftNavigation()).toBe(true);
 }finally{cleanup();detach();f.dispose()}
})
it('explicit save-page action after an unedited hydration remains a real POST followed by original draft cleanup/flush',async()=>{
 const f=fixture(),one=f.create(),detach=one.host.attach();try{
  render(<TextbookLanguageCandidateReview draftHost={one.host} childId="child-primary-4" captureOnly api={f.api}/>);await waitFor(()=>expect(screen.getByLabelText('教材页标题')).not.toBeDisabled());expect(f.puts()).toHaveLength(0);
  fireEvent.click(screen.getByRole('button',{name:'保存本机 OCR 页'}));await waitFor(()=>expect(f.pagePosts()).toBe(1));await waitFor(()=>expect(f.puts().length).toBeGreaterThan(0));await one.host.entry(f.api,'child-primary-4','capture').journal.flush();
  expect(f.stored.get('child-primary-4').page.title).toBe('');expect(f.stored.get('child-junior-3')).toEqual(payload);
 }finally{cleanup();detach();f.dispose()}
})
