// @vitest-environment node
// Actual Node worker + original child pipes. Synthetic framing only; no Windows/DPAPI claim.
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'
import {spawnSync} from 'node:child_process'
import {buildSync} from 'esbuild'
import {afterEach,beforeEach,describe,expect,it,vi} from 'vitest'
import {createDraftCryptoBridge,type DraftCryptoBridge} from './parent-draft-codec-bridge'
let root:string,script:string
const held:DraftCryptoBridge[]=[]
const fixture=String.raw`
const limit=1024,MAGIC=0x48535044,mode=process.argv[2];let data=Buffer.alloc(0),count=0;
const send=(op,status,seq,bytes=Buffer.alloc(0))=>{const h=Buffer.alloc(16);h.writeUInt32BE(MAGIC);h[4]=1;h[5]=op;h[6]=status;h.writeUInt32BE(seq,8);h.writeUInt32BE(bytes.length,12);process.stdout.write(Buffer.concat([h,bytes]));h.fill(0)};
if(mode!=='no-ready')send(0,0,0);
process.stdin.on('data',chunk=>{data=Buffer.concat([data,chunk]);chunk.fill(0);while(data.length>=16){const len=data.readUInt32BE(12);if(data.length<16+len)return;const op=data[5],seq=data.readUInt32BE(8),body=Buffer.from(data.subarray(16,16+len));data.fill(0,0,16+len);data=Buffer.from(data.subarray(16+len));count++;
 if(mode==='hang')continue;
 if(mode==='stderr'){process.stderr.write('synthetic-not-returned');continue}
 if(mode==='late'){setTimeout(()=>{send(op,0,seq,body);body.fill(0)},250);continue}
 if(mode==='wrong-seq'){send(op,0,seq+1,body);body.fill(0);continue}
 if(mode==='wrong-op'){send(op===3?1:3,0,seq,body);body.fill(0);continue}
 if(mode==='bad-header'){process.stdout.write(Buffer.alloc(16));continue}
 if(mode==='overflow'){const h=Buffer.alloc(16);h.writeUInt32BE(MAGIC);h[4]=1;h[5]=op;h.writeUInt32BE(seq,8);h.writeUInt32BE(limit+1,12);process.stdout.write(h);continue}
 if(mode==='bad-status'){send(op,9,seq);continue}
 if(mode==='sized'){if(body[0]===0){send(op,4,seq);body.fill(0);continue}const output=Buffer.alloc(body[0]===1?900:body[0]===2?1:17,0x73);send(op,0,seq,output);body.fill(0);output.fill(0);continue}
 if(mode==='crypto'){send(op,4,seq);continue}
 if(op===3){send(op,0,seq,Buffer.from('HSC_DRAFT_REPLACED'));body.fill(0);continue}
 if(mode==='env'){send(op,0,seq,Buffer.from(Object.keys(process.env).some(x=>/SECRET|TOKEN|HSC_|NODE_OPTIONS/i.test(x))?'leak':'minimal'));continue}
 send(op,0,seq,body);body.fill(0)
}});
process.stdin.on('end',()=>{if(mode==='late')return;if(mode==='ignore-eof'){setInterval(()=>{},1000);return}process.exit(0)});
`
beforeEach(()=>{root=fs.mkdtempSync(path.join(fs.realpathSync(os.tmpdir()),'crypto-worker-public-'));script=path.join(root,'synthetic.cjs');fs.writeFileSync(script,fixture)})
afterEach(()=>{for(const bridge of held.splice(0)){try{bridge.dispose()}catch{}}fs.rmSync(root,{recursive:true,force:true})})
function create(mode='normal',timeoutMs=1500){const b=createDraftCryptoBridge({executable:process.execPath,args:[script,mode],env:{PATH:process.env.PATH},limit:1024,timeoutMs});held.push(b);return b}
function zero(b:DraftCryptoBridge){expect(b.inspect().requestZero).toBe(true);expect(b.inspect().responseZero).toBe(true)}
describe('private worker synchronous original-child bridge',()=>{
 it('serializes many exact operations; clears shared buffers and confirms original child EOF close',()=>{
  const b=create(),input=Buffer.from('public synthetic 🧠');for(let i=0;i<20;i++){const result=b.invoke(i%2?'decrypt':'encrypt',input);expect(result.status).toBe(0);expect(result.output.equals(input)).toBe(true);result.output.fill(0);zero(b)}
  b.dispose();expect(b.inspect()).toMatchObject({closed:true,retired:true,terminated:false,poisoned:true});zero(b);expect(()=>b.invoke('encrypt',input)).toThrow('DRAFT_RECOVERY_IO');b.dispose()
 })
 it('does not inherit unrelated worker/Main environment or put payload in argv',()=>{
  process.env.HSC_TEST_PRIVATE_TOKEN='not-inherited';try{const b=create('env');expect(b.invoke('encrypt',Buffer.from('public')).output.toString()).toBe('minimal')}finally{delete process.env.HSC_TEST_PRIVATE_TOKEN}
 })
 it('preserves explicit crypto status without legacy fallback and can service later serial requests',()=>{
  const b=create('crypto');for(let i=0;i<2;i++){expect(b.invoke('decrypt',Buffer.from('public')).status).toBe(4);zero(b)}
 })
 it.each(['wrong-seq','bad-header','overflow','bad-status','stderr'])('rejects %s with finite IO and retires the original child',mode=>{
  const b=create(mode);expect(()=>b.invoke('encrypt',Buffer.from('public'))).toThrow('DRAFT_RECOVERY_IO');expect(b.inspect()).toMatchObject({poisoned:true,closed:true,retired:true});zero(b);expect(()=>b.invoke('encrypt',Buffer.from('new'))).toThrow('DRAFT_RECOVERY_IO')
 })
 it('times out then permanently poisons; a late response cannot populate shared buffers or become a later ACK',()=>{
  const b=create('late',150);expect(()=>b.invoke('encrypt',Buffer.from('old'))).toThrow('DRAFT_RECOVERY_IO');expect(b.inspect()).toMatchObject({closed:true,retired:true,poisoned:true});zero(b);expect(()=>b.invoke('encrypt',Buffer.from('new'))).toThrow('DRAFT_RECOVERY_IO')
 })
 it('EOF grace expires, kills only the held original child, and requires its actual close event',()=>{
  const b=create('ignore-eof');b.invoke('encrypt',Buffer.from('public'));const start=performance.now();b.dispose();expect(performance.now()-start).toBeLessThan(5000);expect(b.inspect()).toMatchObject({closed:true,retired:true,terminated:true});zero(b)
 })
 it('startup without a ready frame is bounded and never yields an admitted bridge',()=>{const start=performance.now();expect(()=>create('no-ready',150)).toThrow('DRAFT_RECOVERY_IO');expect(performance.now()-start).toBeLessThan(5500)})
 it('rejects oversized input before protocol dispatch; a valid later operation still works',()=>{const b=create();expect(()=>b.invoke('encrypt',Buffer.alloc(1025))).toThrow('DRAFT_RECOVERY_IO');expect(b.invoke('encrypt',Buffer.from('public')).status).toBe(0);zero(b)})
 it('does not inherit a real parent --require preloader into its private Worker',()=>{
  const preload=path.join(root,'parent-only.cjs');fs.writeFileSync(preload,"if(!require('node:worker_threads').isMainThread)throw Error('public parent preloader inherited')")
  const bridgeSource=fs.readFileSync(new URL('./parent-draft-codec-bridge.ts',import.meta.url),'utf8')
  const results:number[]=[]
  for(const strip of [true,false]){
   const module=path.join(root,strip?'unsafe-control.cjs':'isolated.cjs')
   buildSync({stdin:{contents:strip?bridgeSource.replace('execArgv:[],',''):bridgeSource,resolveDir:root,loader:'ts'},bundle:true,platform:'node',format:'cjs',outfile:module,logLevel:'silent'})
   const code="const {createDraftCryptoBridge}=require(process.argv[1]);try{const b=createDraftCryptoBridge({executable:process.execPath,args:[process.argv[2],'normal'],env:{PATH:process.env.PATH},limit:1024,timeoutMs:150});b.invoke('encrypt',Buffer.from('public'));b.dispose();process.stdout.write('ISOLATED')}catch{process.stdout.write('FINITE_IO');process.exitCode=1}"
   const result=spawnSync(process.execPath,['--require',preload,'-e',code,module,script],{env:{PATH:process.env.PATH},timeout:8000,maxBuffer:8192,encoding:'utf8'})
   expect(result.error).toBeUndefined();results.push(result.status??-1)
   expect(result.stdout).toBe(strip?'FINITE_IO':'ISOLATED')
  }
  expect(results).toEqual([1,0])
 },15000)
 it('op3 serial commit ACK shares the exact worker and preserves both crypto operations',()=>{
  const b=create(),text=Buffer.from('public');expect(b.invoke('encrypt',text).output.equals(text)).toBe(true)
  expect(b.invoke('replaceAndFlush',Buffer.from('synthetic bounded metadata')).output.toString()).toBe('HSC_DRAFT_REPLACED');zero(b)
  expect(b.invoke('decrypt',text).output.equals(text)).toBe(true);b.dispose();expect(b.inspect().closed).toBe(true)
 })
 it.each(['crypto','wrong-seq','wrong-op','late'])('op3 %s cannot become a crypto retry or late ACK and poisons the session',mode=>{
  const b=create(mode,mode==='late'?150:1500);expect(()=>b.invoke('replaceAndFlush',Buffer.from('synthetic metadata'))).toThrow('DRAFT_RECOVERY_IO');expect(b.inspect()).toMatchObject({closed:true,poisoned:true});zero(b);expect(()=>b.invoke('encrypt',Buffer.from('later'))).toThrow('DRAFT_RECOVERY_IO')
 })
 it('op3 metadata larger than256 is rejected before dispatch without weakening crypto limit',()=>{const b=create();expect(()=>b.invoke('replaceAndFlush',Buffer.alloc(257))).toThrow('DRAFT_RECOVERY_IO');expect(b.invoke('encrypt',Buffer.alloc(300,1)).status).toBe(0);zero(b)})

 it('normal Main clearing touches only written spans, retaining full zero across large then small responses',()=>{
  const b=create('sized'),spans:number[]=[];
  const fill=vi.spyOn(Uint8Array.prototype,'fill');
  try{
   for(const [input,length] of [[Buffer.alloc(800,1),900],[Buffer.from([2]),1],[Buffer.from([1]),900],[Buffer.alloc(700,3),17]] as const){
    const before=fill.mock.calls.length,result=b.invoke('encrypt',input);expect(result.status).toBe(0);expect(result.output.length).toBe(length);zero(b);result.output.fill(0);
    for(let i=before;i<fill.mock.calls.length;i++){const receiver=fill.mock.contexts[i] as Uint8Array|undefined;if(receiver?.length===1024){const [,start=0,end=receiver.length]=fill.mock.calls[i];spans.push(Number(end)-Number(start))}}
   }
  }finally{fill.mockRestore()}
  expect(spans.length).toBeGreaterThan(0);expect(Math.max(...spans)).toBeLessThan(1024);b.dispose();zero(b);expect(b.inspect()).toMatchObject({closed:true,retired:true,terminated:false})
 })
 it('real worker small request large reply and later crypto-error/zero reply never leave old shared tails',()=>{
  const b=create('sized');for(const request of [Buffer.from([1]),Buffer.from([2]),Buffer.from([0]),Buffer.alloc(999,3),Buffer.from([2])]){
   const result=b.invoke('decrypt',request);expect(result.status).toBe(request[0]===0?4:0);expect(result.output.length).toBe(request[0]===0?0:request[0]===1?900:request[0]===2?1:17);result.output.fill(0);zero(b)
  }b.dispose();zero(b);expect(b.inspect()).toMatchObject({closed:true,retired:true,terminated:false})
 })
 it('late and malformed operation retirement still uses full shared wipes and original child close acknowledgement',()=>{
  for(const mode of ['late','wrong-seq']){const b=create(mode,mode==='late'?150:1500),fill=vi.spyOn(Uint8Array.prototype,'fill');let full=false;
   try{expect(()=>b.invoke('encrypt',Buffer.alloc(900,0x37))).toThrow('DRAFT_RECOVERY_IO');full=fill.mock.calls.some((args,i)=>{const receiver=fill.mock.contexts[i] as Uint8Array|undefined;return receiver?.length===1024&&(args[1]??0)===0&&(args[2]??receiver.length)===1024})}finally{fill.mockRestore()}
   expect(full).toBe(true);zero(b);expect(b.inspect()).toMatchObject({closed:true,retired:true,poisoned:true});expect(()=>b.invoke('encrypt',Buffer.from('later'))).toThrow('DRAFT_RECOVERY_IO')
  }
 })

})
