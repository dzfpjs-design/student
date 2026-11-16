// @vitest-environment node
import {afterEach,expect,it,vi} from 'vitest'
import * as fs from 'node:fs'
import * as path from 'node:path'
import * as os from 'node:os'
import {createDraftCryptoBridgeAsync,type DraftCryptoBridge} from './parent-draft-codec-bridge'
import {ParentDraftRecoveryCoordinator,ParentDraftRecoveryStore,type ParentDraftRecoveryCodec} from './parent-draft-recovery'

const roots:string[]=[],held:DraftCryptoBridge[]=[]
afterEach(async()=>{for(const b of held.splice(0))await b.disposeAsync();for(const r of roots.splice(0))fs.rmSync(r,{recursive:true,force:true})})
function temp(){const p=fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(),'public-draft-ready-')));roots.push(p);return p}
function gate(){let release!:()=>void;const promise=new Promise<void>(r=>{release=r});return {promise,release}}
function state(prepare:()=>Promise<void>){
 const root=temp(),codec:ParentDraftRecoveryCodec={assertAvailable(){},encrypt:v=>Buffer.from(v),decrypt:b=>b.toString()},store=new ParentDraftRecoveryStore({userDataPath:root,profileId:'public-profile',codec})
 const authorize=vi.fn(async()=>({profileId:'public-profile'})),c=new ParentDraftRecoveryCoordinator({store,prepare,authorize})
 return {c,store,authorize}
}
const frameHelper=String.raw`const fs=require('node:fs');let data=Buffer.alloc(0);function send(op,seq,body=Buffer.alloc(0)){const h=Buffer.alloc(16);h.writeUInt32BE(0x48535044);h[4]=1;h[5]=op;h.writeUInt32BE(seq,8);h.writeUInt32BE(body.length,12);process.stdout.write(Buffer.concat([h,body]));}setTimeout(()=>send(0,0),Number(process.argv[2]));process.stdin.on('data',b=>{data=Buffer.concat([data,b]);while(data.length>=16){const n=data.readUInt32BE(12);if(data.length<16+n)return;send(data[5],data.readUInt32BE(8),data.subarray(16,16+n));data=data.subarray(16+n);}});process.stdin.on('end',()=>{});`
it('legal delayed original READY leaves Main timers responsive and later synchronous ACK remains exact',async()=>{
 const root=temp(),script=path.join(root,'helper.cjs');fs.writeFileSync(script,frameHelper)
 let timerRan=false;const timer=setTimeout(()=>{timerRan=true},20)
 const b=await createDraftCryptoBridgeAsync({executable:process.execPath,args:[script,'75'],env:{PATH:process.env.PATH},limit:1024,timeoutMs:2000});held.push(b);clearTimeout(timer)
 expect(timerRan).toBe(true);const input=Buffer.from('public synthetic draft'),r=b.invoke('encrypt',input)
 expect(r.status).toBe(0);expect(r.output.equals(input)).toBe(true);r.output.fill(0)
 await b.disposeAsync();expect(b.inspect()).toMatchObject({closed:true,retired:true,terminated:false,requestZero:true,responseZero:true})
})
it('startup cancellation that requires held-helper force remains HOLD; no late READY is admitted',async()=>{
 const root=temp(),script=path.join(root,'helper.cjs');fs.writeFileSync(script,frameHelper);const controller=new AbortController()
 const pending=createDraftCryptoBridgeAsync({executable:process.execPath,args:[script,'1100'],env:{PATH:process.env.PATH},limit:1024,timeoutMs:2000},controller.signal)
 setTimeout(()=>controller.abort(),30)
 await expect(pending).rejects.toThrow('ORIGINAL_CLOSE_UNCONFIRMED')
})
it('no lease exists while preparing; the original authorization is checked again after READY',async()=>{
 const g=gate(),f=state(()=>g.promise),pending=f.c.prepare(7,'child','capture','public-parent')
 await vi.waitFor(()=>expect(f.authorize).toHaveBeenCalledTimes(1))
 expect(()=>f.c.checkpoint(7,'not-issued',1,{} as any)).toThrow('INVALID_LEASE')
 g.release();const grant=await pending;expect(grant.protection).toBe('native_encrypted');expect(f.authorize).toHaveBeenCalledTimes(2)
})
it.each(['sender-revoke','frame-replacement','quit-blocked-then-resume'])('waiting %s cannot become a newly begun or revived grant',async reason=>{
 const g=gate(),f=state(()=>g.promise);let frame=true
 const old=f.c.prepare(7,'child','capture','public-parent',()=>{if(!frame)throw Error('ORIGINAL_FRAME_CHANGED')});const rejected=expect(old).rejects.toThrow()
 await vi.waitFor(()=>expect(f.authorize).toHaveBeenCalledTimes(1))
 if(reason==='sender-revoke')f.c.revokeSender(7)
 if(reason==='frame-replacement')frame=false
 if(reason==='quit-blocked-then-resume'){f.c.quiesce();f.c.resume()}
 g.release();await rejected
 frame=true;const fresh=await f.c.prepare(7,'child','capture','fresh-parent');expect(fresh.leaseId).toBeTruthy()
})
it('quiesce permits the existing final durable checkpoint but rejects new preparation until resumed',async()=>{
 const f=state(async()=>{}),grant=await f.c.prepare(7,'child','capture','public-parent')
 const snapshot={schema:1 as const,childId:'child',scope:'capture' as const,acknowledged:null,latest:null,pending:null}
 f.c.quiesce();expect(f.c.checkpoint(7,grant.leaseId,1,snapshot).stored).toBe(true)
 await expect(f.c.prepare(7,'child','capture','public-parent')).rejects.toThrow('REVOKED')
 f.c.resume();expect((await f.c.prepare(7,'child','capture','fresh-parent')).leaseId).not.toBe(grant.leaseId)
})
it.each([['frame','prepare'],['frame','read'],['frame','discard'],['quit-and-resume','prepare'],['quit-and-resume','read'],['quit-and-resume','discard']])('the final authorization-to-store microtask cannot grant after %s during %s',async(reason,method)=>{
 const f=state(async()=>{}),read=vi.spyOn(f.store,'read');let frame=true,count=0
 const current=()=>{
  if(!frame)throw Error('ORIGINAL_FRAME_CHANGED')
  if(++count===4)queueMicrotask(()=>{if(reason==='frame')frame=false;else{f.c.quiesce();f.c.resume()}})
 }
 const pending=method==='prepare'?f.c.prepare(7,'child','capture','public-parent',current):method==='read'?f.c.read(7,'child','capture','public-parent',0,current):f.c.discard(7,'child','capture','public-parent',0,true,current)
 await expect(pending).rejects.toThrow();expect(read).not.toHaveBeenCalled()
})
it('mismatched first authorization cannot begin public codec preparation',async()=>{
 const warm=vi.fn(async()=>{}),f=state(warm);f.authorize.mockResolvedValueOnce({profileId:'wrong-profile'})
 await expect(f.c.prepare(7,'child','capture','public-parent')).rejects.toThrow('UNAUTHORIZED');expect(warm).not.toHaveBeenCalled()
})
