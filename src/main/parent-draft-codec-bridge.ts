import { Worker } from 'node:worker_threads'

const LIMIT = 4259840
const INIT=0, READY=1, REQUEST=2, REPLY=3, CLOSING=4, CLOSED=5, POISON=6
export type DraftCryptoBridge = {
  invoke(operation:'encrypt'|'decrypt'|'replaceAndFlush', input:Buffer): { status:number; output:Buffer }
  dispose():void
  disposeAsync():Promise<void>
  inspect(): {closed:boolean;terminated:boolean;retired:boolean;requestZero:boolean;responseZero:boolean;poisoned:boolean}
}
export type DraftCryptoBridgeOptions={executable:string;args:string[];env:NodeJS.ProcessEnv;limit?:number;timeoutMs?:number}

// One private worker/one original ChildProcess. No payload in messages/argv/logs.
export const draftCryptoWorkerSource=String.raw`
'use strict';
const {parentPort,workerData}=require('node:worker_threads');
const {spawn}=require('node:child_process');
const INIT=0,READY=1,REQUEST=2,REPLY=3,CLOSING=4,CLOSED=5,POISON=6;
const c=new Int32Array(workerData.control),request=new Uint8Array(workerData.request),response=new Uint8Array(workerData.response);
const limit=workerData.limit,timeout=workerData.timeoutMs,MAGIC=0x48535044;
let child=null,finished=false,closing=false,ready=false,busy=null,used=0,wanted=16,header=null,operationTimer=null,closeTimer=null,startTimer=null,outgoing=null;
const incoming=Buffer.alloc(limit+16);
const zero=()=>{request.fill(0);response.fill(0);incoming.fill(0);if(outgoing)outgoing.fill(0)};
const clear=()=>{clearTimeout(operationTimer);clearTimeout(startTimer);operationTimer=startTimer=null};
const retire=()=>{if(closing)return;closing=true;clear();zero();if(!child){finish(null);return}try{child.stdin.end()}catch{}closeTimer=setTimeout(()=>{try{Atomics.store(c,8,1);child.kill()}catch{}},Math.min(timeout,500))};
const fail=()=>{if(Atomics.load(c,0)!==CLOSING)Atomics.store(c,0,POISON);Atomics.notify(c,0);retire()};
function finish(code){if(finished)return;finished=true;clear();clearTimeout(closeTimer);closeTimer=null;zero();Atomics.store(c,6,1);Atomics.store(c,7,Number.isInteger(code)?code:-1);Atomics.store(c,9,1);Atomics.store(c,0,CLOSED);Atomics.notify(c,0);parentPort.close()}
function frame(){
 const op=incoming[5],status=incoming[6],seq=incoming.readUInt32BE(8),length=incoming.readUInt32BE(12);
 if(!ready){if(op!==0||status!==0||seq!==0||length!==0||Atomics.load(c,0)!==INIT)throw Error();ready=true;clearTimeout(startTimer);startTimer=null;Atomics.store(c,0,READY);Atomics.notify(c,0);return}
 if(!busy||op!==busy.op||seq!==busy.seq||![0,2,3,4,5,6].includes(status)||(status===0?(length<1||length>limit):length!==0))throw Error();
 clearTimeout(operationTimer);operationTimer=null; if(op===3&&status!==0){fail();return}
 if(closing||Atomics.load(c,0)!==REQUEST||Atomics.load(c,1)!==seq){zero();retire();return}
 if(status!==0)zero();if(length)response.set(incoming.subarray(16,16+length));
 if(closing||Atomics.load(c,0)!==REQUEST||Atomics.load(c,1)!==seq){zero();retire();return}
 Atomics.store(c,4,status);Atomics.store(c,5,length);busy=null;if(Atomics.compareExchange(c,0,REQUEST,REPLY)!==REQUEST){zero();retire();return}Atomics.notify(c,0);
}
try{
 child=spawn(workerData.executable,workerData.args,{env:workerData.env,windowsHide:true,shell:false,stdio:['pipe','pipe','pipe']});
 child.on('error',fail);child.on('close',code=>finish(code));
 child.stderr.on('data',chunk=>{chunk.fill(0);fail()});
 child.stdout.on('data',chunk=>{try{if(closing)return;let pos=0;while(pos<chunk.length){const count=Math.min(wanted-used,chunk.length-pos);chunk.copy(incoming,used,pos,pos+count);used+=count;pos+=count;if(used===16&&header===null){if(incoming.readUInt32BE(0)!==MAGIC||incoming[4]!==1||incoming[7]!==0)throw Error();const n=incoming.readUInt32BE(12);if(n>limit)throw Error();header=true;wanted=16+n}if(header&&used===wanted){frame();incoming.fill(0,0,used);used=0;wanted=16;header=null;if(closing)return}}}catch{fail()}finally{chunk.fill(0)}});
 startTimer=setTimeout(fail,timeout);
 parentPort.on('message',message=>{try{
  if(message?.type==='dispose'){retire();return}
  if(message?.type!=='operate'||closing||!ready||busy||Atomics.load(c,0)!==REQUEST||message.sequence!==Atomics.load(c,1))throw Error();
  const seq=Atomics.load(c,1),op=Atomics.load(c,2),length=Atomics.load(c,3);if(seq<1||![1,2,3].includes(op)||length<1||length>limit||(op===3&&length>256))throw Error();
  busy={seq,op};outgoing=Buffer.alloc(16+length);outgoing.writeUInt32BE(MAGIC,0);outgoing[4]=1;outgoing[5]=op;outgoing.writeUInt32BE(seq,8);outgoing.writeUInt32BE(length,12);outgoing.set(request.subarray(0,length),16);request.fill(0,0,length);
  const held=outgoing;operationTimer=setTimeout(fail,timeout);child.stdin.write(held,error=>{held.fill(0);if(outgoing===held)outgoing=null;if(error)fail()});
 }catch{fail()}});
}catch{fail()}
process.on('uncaughtException',fail);process.on('unhandledRejection',fail);
`

function startDraftCryptoBridge(options:DraftCryptoBridgeOptions) {
  const limit=options.limit??LIMIT,timeout=options.timeoutMs??2000
  if(!Number.isSafeInteger(limit)||limit<1||limit>LIMIT||!Number.isSafeInteger(timeout)||timeout<1||timeout>2000)throw Error('DRAFT_RECOVERY_IO')
  const control=new SharedArrayBuffer(12*4),requestBuffer=new SharedArrayBuffer(limit),responseBuffer=new SharedArrayBuffer(limit)
  const c=new Int32Array(control),request=new Uint8Array(requestBuffer),response=new Uint8Array(responseBuffer)
  let worker:Worker,poisoned=false,busy=false,disposed=false,sequence=0
  let exitCode:number|undefined,resolveExit!:(code:number)=>void
  const originalExit=new Promise<number>(resolve=>{resolveExit=resolve})
  const zero=()=>{request.fill(0);response.fill(0)}
  // Fresh SABs start entirely zero; READY is published only after both previous written spans are cleared.
  const zeroUsed=(requestLength:number,responseLength:number)=>{request.fill(0,0,requestLength);response.fill(0,0,responseLength)}
  const waitState=(accept:(state:number)=>boolean,budget=timeout):boolean=>{const until=performance.now()+budget;for(;;){const state=Atomics.load(c,0);if(accept(state))return true;const left=until-performance.now();if(left<=0)return false;Atomics.wait(c,0,state,left)}}
  const waitAsync=(accept:(state:number)=>boolean,budget:number,signal?:AbortSignal):Promise<boolean>=>new Promise(resolve=>{
    const until=performance.now()+budget
    const check=()=>{if(signal?.aborted){resolve(false);return};if(accept(Atomics.load(c,0))){resolve(true);return};const left=until-performance.now();if(left<=0){resolve(false);return};setTimeout(check,Math.min(5,left))};check()
  })
  const dispose=()=>{if(disposed)return;poisoned=true;if(Atomics.load(c,0)!==CLOSED){Atomics.store(c,0,CLOSING);try{worker.postMessage({type:'dispose'})}catch{}}const closed=waitState(state=>state===CLOSED,5000)&&Atomics.load(c,6)===1&&Atomics.load(c,9)===1;zero();if(!closed)throw Error('DRAFT_RECOVERY_IO');disposed=true}
  try{worker=new Worker(draftCryptoWorkerSource,{eval:true,env:options.env,execArgv:[],workerData:{control,request:requestBuffer,response:responseBuffer,executable:options.executable,args:options.args,env:options.env,limit,timeoutMs:timeout}})}catch{zero();throw Error('DRAFT_RECOVERY_IO')}
  worker.once('exit',code=>{exitCode=code;resolveExit(code)})
  worker.on('error',()=>{poisoned=true;if(Atomics.load(c,0)!==CLOSED)Atomics.store(c,0,POISON);Atomics.notify(c,0)})
  worker.unref()
  const disposeAsync=async()=>{
    const until=performance.now()+5000
    if(!disposed){poisoned=true;if(Atomics.load(c,0)!==CLOSED){Atomics.store(c,0,CLOSING);try{worker.postMessage({type:'dispose'})}catch{}}
      const closed=await waitAsync(state=>state===CLOSED,Math.max(0,until-performance.now()))
      zero();if(!closed||Atomics.load(c,6)!==1||Atomics.load(c,9)!==1)throw Error('DRAFT_RECOVERY_IO');disposed=true}
    if(exitCode===undefined){let timer:ReturnType<typeof setTimeout>|undefined;try{await Promise.race([originalExit,new Promise<never>((_resolve,reject)=>{timer=setTimeout(()=>reject(Error('DRAFT_RECOVERY_IO')),Math.max(0,until-performance.now()))})])}finally{clearTimeout(timer)}}
    if(exitCode!==0)throw Error('DRAFT_RECOVERY_IO')
    if(Atomics.load(c,7)!==0||Atomics.load(c,8)!==0)throw Error('DRAFT_RECOVERY_ORIGINAL_CLOSE_UNCONFIRMED')
  }
  const bridge:DraftCryptoBridge={
    invoke(operation,input){
      if(disposed||poisoned||busy||Atomics.load(c,0)!==READY||!['encrypt','decrypt','replaceAndFlush'].includes(operation)||!Buffer.isBuffer(input)||input.length<1||input.length>limit||(operation==='replaceAndFlush'&&input.length>256)||sequence>=0x7fffffff)throw Error('DRAFT_RECOVERY_IO')
      busy=true;const seq=++sequence
      try{
        request.set(input);Atomics.store(c,1,seq);Atomics.store(c,2,operation==='encrypt'?1:operation==='decrypt'?2:3);Atomics.store(c,3,input.length);Atomics.store(c,4,0);Atomics.store(c,5,0);Atomics.store(c,0,REQUEST);worker.postMessage({type:'operate',sequence:seq})
        if(!waitState(state=>state===REPLY||state===CLOSED||state===POISON)||Atomics.load(c,0)!==REPLY||Atomics.load(c,1)!==seq){poisoned=true;Atomics.store(c,0,POISON);try{dispose()}catch{}throw Error('DRAFT_RECOVERY_IO')}
        const status=Atomics.load(c,4),length=Atomics.load(c,5)
        if(![0,2,3,4,5,6].includes(status)||(status===0?(length<1||length>limit):length!==0)){poisoned=true;try{dispose()}catch{}throw Error('DRAFT_RECOVERY_IO')}
        const output=Buffer.from(response.subarray(0,length));if(status===0)zeroUsed(input.length,length);else zero();if(Atomics.compareExchange(c,0,REPLY,READY)!==REPLY){output.fill(0);poisoned=true;try{dispose()}catch{}throw Error('DRAFT_RECOVERY_IO')}
        if(status!==0&&status!==4){output.fill(0);try{dispose()}catch{}throw Error('DRAFT_RECOVERY_IO')}
        return {status,output}
      }catch{poisoned=true;if(Atomics.load(c,0)!==CLOSED)Atomics.store(c,0,POISON);zero();try{dispose()}catch{}throw Error('DRAFT_RECOVERY_IO')}finally{busy=false}
    },
    dispose,disposeAsync,
    inspect:()=>({closed:Atomics.load(c,6)===1,terminated:Atomics.load(c,8)===1,retired:Atomics.load(c,9)===1,requestZero:request.every(x=>x===0),responseZero:response.every(x=>x===0),poisoned})
  }
  return {bridge,
    readySync(){if(!waitState(state=>state===READY||state===CLOSED||state===POISON)||Atomics.load(c,0)!==READY){try{dispose()}catch{}throw Error('DRAFT_RECOVERY_IO')}return bridge},
    async readyAsync(signal?:AbortSignal){
      const ready=await waitAsync(state=>state===READY||state===CLOSED||state===POISON,timeout,signal)
      if(!ready||signal?.aborted||Atomics.load(c,0)!==READY){try{await disposeAsync()}catch{throw Error('DRAFT_RECOVERY_ORIGINAL_CLOSE_UNCONFIRMED')};throw Error(signal?.aborted?'DRAFT_RECOVERY_PREPARATION_CANCELLED':'DRAFT_RECOVERY_IO')}
      return bridge
    }
  }
}
export function createDraftCryptoBridge(options:DraftCryptoBridgeOptions):DraftCryptoBridge{return startDraftCryptoBridge(options).readySync()}
export function createDraftCryptoBridgeAsync(options:DraftCryptoBridgeOptions,signal?:AbortSignal):Promise<DraftCryptoBridge>{return startDraftCryptoBridge(options).readyAsync(signal)}
