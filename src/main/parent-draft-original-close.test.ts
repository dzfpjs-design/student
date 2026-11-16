// @vitest-environment node
// Real original Worker + its real Node helper; synthetic framing, no Windows/DPAPI.
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'
const observed=vi.hoisted(()=>({workers:[] as {exit:number|null}[]}))
vi.mock('node:worker_threads',async()=>{
 const original=await vi.importActual<any>('node:worker_threads')
 return {...original,Worker:function(...args:any[]){
  const worker=new original.Worker(...args),record={exit:null as number|null};observed.workers.push(record)
  worker.once('exit',(code:number)=>{record.exit=code});return worker
 }}
})
import {createDraftCryptoBridge,type DraftCryptoBridge} from './parent-draft-codec-bridge'
let root:string,script:string,receipt:string
const held:DraftCryptoBridge[]=[]
const helper=String.raw`const fs=require('node:fs'),mode=process.argv[2],receipt=process.argv[3];
const h=Buffer.alloc(16);h.writeUInt32BE(0x48535044);h[4]=1;process.stdout.write(h);
process.stdin.on('data',()=>{});
process.stdin.on('end',()=>{fs.writeFileSync(receipt,JSON.stringify({eof:true,planned_exit:mode==='nonzero'?3:mode==='ignore-eof'?null:0}));if(mode==='ignore-eof'){setInterval(()=>{},1000);return}process.exit(mode==='nonzero'?3:0)});`
beforeEach(()=>{root=fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(),'public-original-draft-close-')));script=path.join(root,'helper.cjs');receipt=path.join(root,'public-receipt.json');fs.writeFileSync(script,helper);observed.workers=[]})
afterEach(async()=>{for(const b of held.splice(0)){try{await b.disposeAsync()}catch{}}expect(observed.workers.every(w=>w.exit===0)).toBe(true);fs.rmSync(root,{recursive:true,force:true})})
function create(mode:string){const b=createDraftCryptoBridge({executable:process.execPath,args:[script,mode,receipt],env:{PATH:process.env.PATH},limit:1024,timeoutMs:1500});held.push(b);return b}
function workerClosed(){expect(observed.workers).toHaveLength(1);expect(observed.workers[0].exit).toBe(0)}
it('EOF natural helper zero plus consumed original Worker exit permits async disposal',async()=>{
 const b=create('normal');await expect(b.disposeAsync()).resolves.toBeUndefined();workerClosed()
 expect(JSON.parse(fs.readFileSync(receipt,'utf8'))).toEqual({eof:true,planned_exit:0});expect(b.inspect()).toMatchObject({closed:true,retired:true,terminated:false,requestZero:true,responseZero:true})
})
it('EOF natural helper nonzero remains HOLD even when its original Worker naturally exits zero',async()=>{
 const b=create('nonzero');await expect(b.disposeAsync()).rejects.toThrow('ORIGINAL_CLOSE_UNCONFIRMED');workerClosed()
 expect(JSON.parse(fs.readFileSync(receipt,'utf8'))).toEqual({eof:true,planned_exit:3});expect(b.inspect()).toMatchObject({closed:true,retired:true,terminated:false})
})
it('ignored EOF and original held-helper kill cannot become natural closure through Worker zero',async()=>{
 const b=create('ignore-eof');await expect(b.disposeAsync()).rejects.toThrow('ORIGINAL_CLOSE_UNCONFIRMED');workerClosed()
 expect(JSON.parse(fs.readFileSync(receipt,'utf8'))).toEqual({eof:true,planned_exit:null});expect(b.inspect()).toMatchObject({closed:true,retired:true,terminated:true})
})
it('a previous synchronous dispose cannot hide forced helper closure from the later async exit gate',async()=>{
 const b=create('ignore-eof');b.dispose();expect(b.inspect()).toMatchObject({closed:true,retired:true,terminated:true})
 await expect(b.disposeAsync()).rejects.toThrow('ORIGINAL_CLOSE_UNCONFIRMED');workerClosed()
 expect(b.inspect()).toMatchObject({requestZero:true,responseZero:true})
})
