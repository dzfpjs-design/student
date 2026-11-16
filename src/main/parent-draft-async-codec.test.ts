// @vitest-environment node
// Actual async compiler-shaped Node child + actual bridge worker/helper. No Windows/DPAPI claim.
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import * as fs from 'node:fs'
import * as path from 'node:path'
import * as os from 'node:os'
import {createWindowsDraftCodecPreparation} from './parent-draft-windows-codec'
const observed=vi.hoisted(()=>({noClose:false,rows:[] as any[],onCompilerStarted:undefined as (()=>void)|undefined}))
vi.mock('node:child_process',async()=>{const real=await vi.importActual<any>('node:child_process'),{EventEmitter}=await import('node:events'),{PassThrough}=await import('node:stream');return {...real,spawn:(...args:any[])=>{
 if(observed.noClose){const child=new EventEmitter() as any;child.stdout=new PassThrough();child.stderr=new PassThrough();child.kill=vi.fn(()=>false);observed.rows.push({virtual:true,child});return child}
 const child=real.spawn(...args),row:any={virtual:false,exit:null,close:null};observed.rows.push(row);child.stdout.on('data',(b:Buffer)=>{if(b.length===1&&b[0]===83)observed.onCompilerStarted?.()});child.once('exit',(code:number|null,signal:string|null)=>{row.exit={code,signal}});child.once('close',(code:number|null,signal:string|null)=>{row.close={code,signal}});return child
}}})
const originalPlatform=Object.getOwnPropertyDescriptor(process,'platform')!
const handles:ReturnType<typeof createWindowsDraftCodecPreparation>[]=[],roots:string[]=[]
let base:string,userData:string,count:string
const helper=String.raw`let data=Buffer.alloc(0);function send(op,seq,body=Buffer.alloc(0)){const h=Buffer.alloc(16);h.writeUInt32BE(0x48535044);h[4]=1;h[5]=op;h.writeUInt32BE(seq,8);h.writeUInt32BE(body.length,12);process.stdout.write(Buffer.concat([h,body]));}setTimeout(()=>send(0,0),50);process.stdin.on('data',b=>{data=Buffer.concat([data,b]);while(data.length>=16){const n=data.readUInt32BE(12);if(data.length<16+n)return;send(data[5],data.readUInt32BE(8),data.subarray(16,16+n));data=data.subarray(16+n);}});process.stdin.on('end',()=>{});`
beforeEach(()=>{
 observed.noClose=false;observed.rows=[];observed.onCompilerStarted=undefined
 base=fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(),'public-async-csc-')));roots.push(base);userData=path.join(base,'profile');fs.mkdirSync(userData);count=path.join(base,'compiler-count')
 const framework=path.join(base,'Microsoft.NET','Framework64','v4.0.30319');fs.mkdirSync(framework,{recursive:true});fs.writeFileSync(path.join(framework,'System.Security.dll'),'public synthetic prerequisite')
 const compiler='#!'+process.execPath+'\n'+`const fs=require('node:fs');fs.appendFileSync(${JSON.stringify(count)},'1');const out=process.argv.find(x=>x.startsWith('/out:')).slice(5);setTimeout(()=>{fs.writeFileSync(out,${JSON.stringify('#!'+process.execPath+'\n'+helper)},{mode:0o700});},75);`
 fs.writeFileSync(path.join(framework,'csc.exe'),compiler,{mode:0o700});Object.defineProperty(process,'platform',{value:'win32'});vi.stubEnv('SystemRoot',base)
})
afterEach(async()=>{for(const h of handles.splice(0))await h.dispose();for(const r of observed.rows.filter(r=>!r.virtual)){expect(r.exit).not.toBeNull();expect(r.close).toEqual(r.exit)};vi.useRealTimers();Object.defineProperty(process,'platform',originalPlatform);vi.unstubAllEnvs();for(const p of roots.splice(0))fs.rmSync(p,{recursive:true,force:true})})
function make(remember=true){const h=createWindowsDraftCodecPreparation({helperDirectory:path.join(userData,'helper'),userDataPath:userData,profileId:'a'.repeat(64),legacyCodec:{assertAvailable(){},encrypt:()=>{throw Error('legacy forbidden')},decrypt:()=>{throw Error('legacy forbidden')}}});if(remember)handles.push(h);return h}
it('cold handle/checkpoint access cannot spawn; simultaneous preparation shares one real compiler and READY',async()=>{
 const h=make();expect(()=>h.current()).toThrow('IO');expect(fs.existsSync(count)).toBe(false)
 const a=h.prepare(),b=h.prepare();expect(a).toBe(b);let responsive=false;setTimeout(()=>{responsive=true},20)
 const codec=await a;expect(responsive).toBe(true);expect(fs.readFileSync(count,'utf8')).toBe('1')
 const cipher=codec.encrypt('public synthetic draft');expect(codec.decrypt(cipher)).toBe('public synthetic draft');cipher.fill(0)
 await h.quiesce();expect(h.current()).toBe(codec);expect(codec.decrypt(codec.encrypt('final unload ACK'))).toBe('final unload ACK')
 await expect(h.prepare()).rejects.toThrow('CANCELLED');h.resume();expect(await h.prepare()).toBe(codec)
})
it('quit cancellation consumes the original compiler close; resume creates a new attempt, never revives old preparation',async()=>{
 const compiler=path.join(base,'Microsoft.NET','Framework64','v4.0.30319','csc.exe'),normal=fs.readFileSync(compiler)
 let signalStarted!:()=>void;const started=new Promise<void>(resolve=>{signalStarted=resolve});observed.onCompilerStarted=signalStarted
 fs.writeFileSync(compiler,'#!'+process.execPath+'\n'+`const fs=require('node:fs');fs.appendFileSync(${JSON.stringify(count)},'1');process.stdout.write(Buffer.from([83]));setInterval(()=>{},1000);`,{mode:0o700})
 const h=make(),old=h.prepare()
 await Promise.race([started,old.then(()=>{throw Error('SYNTHETIC_COMPILER_READY_BEFORE_CANCEL_ACK')})])
 expect(fs.readFileSync(count,'utf8')).toBe('1');await h.quiesce();await expect(old).rejects.toThrow('CANCELLED')
 expect(observed.rows[0].exit).not.toBeNull();expect(observed.rows[0].close).toEqual(observed.rows[0].exit)
 expect(()=>h.current()).toThrow('IO');fs.writeFileSync(compiler,normal,{mode:0o700});h.resume();const fresh=await h.prepare();expect(fresh).toBe(h.current());expect(fs.readFileSync(count,'utf8')).toBe('11')
})

it('compiler failure consumes its original exit and close, grants no codec, and does not silently retry',async()=>{
 const compiler=path.join(base,'Microsoft.NET','Framework64','v4.0.30319','csc.exe');fs.writeFileSync(compiler,'#!'+process.execPath+'\nprocess.exitCode=2;',{mode:0o700})
 const h=make(),old=h.prepare();await expect(old).rejects.toThrow('ENCRYPTION_UNAVAILABLE');expect(()=>h.current()).toThrow('IO');expect(h.prepare()).toBe(old);expect(observed.rows).toHaveLength(1)
 expect(observed.rows[0].exit).toEqual({code:2,signal:null});expect(observed.rows[0].close).toEqual({code:2,signal:null})
})
it('unconfirmed original compiler close is bounded, poisoned, and cannot be replaced after resume',async()=>{
 vi.useFakeTimers();observed.noClose=true;const h=make(false),pending=h.prepare(),rejected=expect(pending).rejects.toThrow('ORIGINAL_CLOSE_UNCONFIRMED')
 const draining=h.quiesce(),blocked=expect(draining).rejects.toThrow('ORIGINAL_CLOSE_UNCONFIRMED');await vi.advanceTimersByTimeAsync(5001);await rejected;await blocked
 h.resume();expect(h.prepare()).toBe(pending);expect(()=>h.current()).toThrow('IO');expect(observed.rows).toHaveLength(1);expect(observed.rows[0].child.kill).toHaveBeenCalledTimes(1)
 // This is an explicit virtual missing-close negative, not native cleanup evidence.
})
