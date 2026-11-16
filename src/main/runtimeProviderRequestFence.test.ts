// @vitest-environment node
import { expect, it, vi } from 'vitest'
import { EventEmitter } from 'node:events'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { runInNewContext } from 'node:vm'
import { parse } from '@babel/parser'
import { transformSync } from 'esbuild'
import { RuntimeProviderRefresh } from './runtime-provider-refresh'
vi.mock('electron', () => ({ safeStorage: {}, app: {} }))
import { exchangeQwenRealtimeSdp, defaultQwenRealtimeConfig } from './qwen-realtime'
const offer = `v=0\r\n${'a'.repeat(80)}\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n`
const answer = `v=0\r\n${'b'.repeat(80)}\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n`
const config = defaultQwenRealtimeConfig('public-synthetic-stage-key', 'https://ws-test.cn-beijing.maas.aliyuncs.com')
function deferred<T>() { let resolve!: (value:T)=>void; const promise = new Promise<T>(r => { resolve=r }); return { promise, resolve } }
function handlerText(channel = 'voice:realtime-negotiate') {
 const raw = readFileSync(resolve('src/main/index.ts'), 'utf8')
 const ast = parse(raw,{sourceType:'module',plugins:['typescript']}); let found:string|undefined
 function walk(node:any) { if(!node||typeof node!=='object')return
  if(node.type==='CallExpression'&&node.callee?.object?.name==='ipcMain'&&node.callee?.property?.name==='handle'&&node.arguments[0]?.value===channel) found=raw.slice(node.arguments[1].start,node.arguments[1].end)
  for(const v of Object.values(node)) if(Array.isArray(v))v.forEach(walk);else if(v&&typeof v==='object')walk(v)
 }
 walk(ast);if(!found)throw Error('actual handler missing')
 return transformSync('handler='+found,{loader:'ts',target:'es2022'}).code
}
function fixture() {
 const entered=deferred<void>(), loaded=deferred<typeof config>()
 const sender = Object.assign(new EventEmitter(), { id:1, destroyed:false, isDestroyed(){return this.destroyed} })
 const window={ webContents:sender,isDestroyed:()=>false }
 const provider = new RuntimeProviderRefresh()
 const fetchImpl=vi.fn(async()=>new Response(answer,{status:200}))
 const exchange=vi.fn((c:typeof config,o:string,_f:unknown,s?:AbortSignal)=>exchangeQwenRealtimeSdp(c,o,fetchImpl as typeof fetch,s))
 const context:any={ readVoiceRuntimePolicy:vi.fn(async()=>({version:1,capture:'configured',runtimeEpoch:1})), primaryWindow:window, cloudProviders:provider, qwenRealtimeSettings:{load:vi.fn(()=>{entered.resolve();return loaded.promise})}, exchangeQwenRealtimeSdp:exchange,
  diagnosticRecorder:{record:vi.fn(async()=>undefined)},AbortController,AbortSignal,DOMException,Date,Error }
 runInNewContext(handlerText(),context)
 return { context,provider,sender,window,entered,loaded,fetchImpl,exchange,run:()=>context.handler({sender},offer), cleanup:()=>expect(['did-start-navigation','destroyed','render-process-gone'].map(x=>sender.listenerCount(x))).toEqual([0,0,0]) }
}
const dependencies=()=>({assertCurrent:()=>{}, fingerprint:async()=>'fixed',loadSnapshot:async()=>({modelConfig:null,asrConfig:null,voiceConfig:null}),admit:()=>1,apply:async()=>2,applied:()=>true,result:()=> 'ready'})
it.each(['reload','save','clear'])('actual old-config-wait handler cannot send old SDP using stage key after %s',async(kind)=>{
 const f=fixture();const outcome=f.run().then((value:any)=>({value}),(error:any)=>({error}))
 await f.entered.promise
 if(kind==='reload')await f.provider.reload(dependencies());else await f.provider.mutate(async()=>kind)
 f.loaded.resolve(config);const result=await outcome
 expect(f.fetchImpl).not.toHaveBeenCalled();expect(result).toHaveProperty('error.name','AbortError');f.cleanup()
})
it.each(['main-navigation','destroyed','render-process-gone'])('same sender %s cancels a deferred settings read without forwarding',async(kind)=>{
 const f=fixture();const result=f.run().catch((e:any)=>e);await f.entered.promise
 if(kind==='main-navigation')f.sender.emit('did-start-navigation',{},'public://fixture',false,true)
 else { if(kind==='destroyed')f.sender.destroyed=true;f.sender.emit(kind,{}) }
 f.loaded.resolve(config);expect(await result).toHaveProperty('name','AbortError');expect(f.fetchImpl).not.toHaveBeenCalled();f.cleanup()
})
it('subframe/in-place navigation preserves normal signaling and removes only owned temporary listeners',async()=>{
 const f=fixture();const external=()=>{};f.sender.on('destroyed',external)
 const result=f.run();await f.entered.promise
 f.sender.emit('did-start-navigation',{},'public://subframe',false,false);f.sender.emit('did-start-navigation',{},'public://same-document',true,true)
 f.loaded.resolve(config);expect(await result).toHaveProperty('answerSdp',answer);expect(f.fetchImpl).toHaveBeenCalledOnce()
 expect(f.sender.listeners('destroyed')).toEqual([external]);f.sender.removeListener('destroyed',external);f.cleanup()
})
it('same-id replacement primary is rejected after load',async()=>{
 const f=fixture();const result=f.run().catch((e:any)=>e);await f.entered.promise
 f.context.primaryWindow={webContents:f.sender,isDestroyed:()=>false};f.loaded.resolve(config)
 expect(await result).toHaveProperty('name','AbortError');expect(f.fetchImpl).not.toHaveBeenCalled();f.cleanup()
})
it('foreign sender is rejected before settings reads',async()=>{
 const f=fixture();await expect(f.context.handler({sender:{id:1,isDestroyed:()=>false}},offer)).rejects.toThrow('窗口无效')
 expect(f.context.qwenRealtimeSettings.load).not.toHaveBeenCalled();f.cleanup()
})
it.each(['reload','save','clear'])('%s request synchronously aborts an active lease and rejects new leases until settled',async(kind)=>{
 const provider=new RuntimeProviderRefresh(),held=deferred<void>(),entered=deferred<void>();let signal!:AbortSignal
 const old=provider.withCurrentProviderRequest(async(s)=>{signal=s;entered.resolve();await held.promise;return'late'}).catch(e=>e);await entered.promise
 const pending=deferred<string>();const mutation=kind==='reload'?provider.reload({...dependencies(),loadSnapshot:async()=>{await pending.promise;return{modelConfig:null,asrConfig:null,voiceConfig:null}}}):provider.mutate(()=>pending.promise)
 expect(signal.aborted).toBe(true);const callback=vi.fn(async()=> 'should-not-run')
 await expect(provider.withCurrentProviderRequest(callback)).rejects.toHaveProperty('name','AbortError');expect(callback).not.toHaveBeenCalled()
 pending.resolve('finished');await mutation;held.resolve();expect(await old).toHaveProperty('name','AbortError')
 expect(await provider.withCurrentProviderRequest(async s=>{expect(s.aborted).toBe(false);return'current'})).toBe('current')
 expect((provider as any).providerRequests.size).toBe(0)
})
it('failed/queued reloads do not reopen request admission or change cache revision semantics',async()=>{
 const provider=new RuntimeProviderRefresh(),held=deferred<string>();const first=provider.mutate(()=>held.promise)
 const second=provider.reload({...dependencies(),admit:()=>{throw Error('busy')}}).catch(e=>e)
 expect((provider as any).revision).toBe(1);await expect(provider.withCurrentProviderRequest(async()=>0)).rejects.toHaveProperty('name','AbortError')
 held.resolve('saved');await first;expect(await second).toHaveProperty('message','busy');expect((provider as any).revision).toBe(2)
 expect((provider as any).pendingReloads).toBe(0);expect(await provider.withCurrentProviderRequest(async()=> 'retry')).toBe('retry')
})
it('request errors and completion remove controllers; future changes do not abort a completed signal',async()=>{
 const provider=new RuntimeProviderRefresh();let old!:AbortSignal
 expect(await provider.withCurrentProviderRequest(async s=>{old=s;return'ready'})).toBe('ready')
 await expect(provider.withCurrentProviderRequest(async()=>{throw Error('known') })).rejects.toThrow('known')
 expect((provider as any).providerRequests.size).toBe(0);await provider.mutate(async()=> 'done');expect(old.aborted).toBe(false)
})
it('actual handler rejects a late exchange that ignores owner abort, with no success diagnostic',async()=>{
 const f=fixture();const exchange=deferred<any>(),entered=deferred<void>();f.context.exchangeQwenRealtimeSdp=vi.fn((_c,_o,_f,signal:AbortSignal)=>{entered.resolve();expect(signal.aborted).toBe(false);return exchange.promise})
 const result=f.run().catch((e:any)=>e);f.loaded.resolve(config);await entered.promise
 f.sender.emit('render-process-gone',{});exchange.resolve({answerSdp:answer,model:config.model,voice:config.voice})
 expect(await result).toHaveProperty('name','AbortError');expect(f.context.diagnosticRecorder.record.mock.calls.map((c:any)=>c[0].event)).not.toContain('voice.realtime.signaling_succeeded');f.cleanup()
})
it('actual handler rejects a late exchange that ignores provider reload abort',async()=>{
 const f=fixture();const exchange=deferred<any>(),entered=deferred<void>();let signal!:AbortSignal
 f.context.exchangeQwenRealtimeSdp=vi.fn((_c,_o,_f,s:AbortSignal)=>{signal=s;entered.resolve();return exchange.promise})
 const result=f.run().catch((e:any)=>e);f.loaded.resolve(config);await entered.promise;await f.provider.reload(dependencies());expect(signal.aborted).toBe(true)
 exchange.resolve({answerSdp:answer,model:config.model,voice:config.voice});expect(await result).toHaveProperty('name','AbortError');f.cleanup()
})
it.each(['voice:realtime-save-settings','voice:realtime-clear-settings'])('actual %s wrapper invalidates old requests before its first storage await',async(channel)=>{
 const f=fixture();const old=f.run().catch((e:any)=>e);await f.entered.promise
 const held=deferred<any>();let atFirstAwait=false
 f.context.process={env:{},platform:'synthetic'}
 Object.assign(f.context.qwenRealtimeSettings,{resolveApiKey:vi.fn(()=>{atFirstAwait=true;return held.promise}),clear:vi.fn(()=>{atFirstAwait=true;return held.promise}),save:vi.fn(async()=>config)})
 f.context.validateQwenRealtimeConfig=(v:any)=>v;f.context.testQwenRealtimeConfig=vi.fn(async()=>({ok:true}));f.context.voiceSettings={loadEffective:vi.fn(async()=>null)}
 f.context.localService={setVoiceConfig:vi.fn()};f.context.restartChangedCloudProviders=vi.fn(async()=>{});f.context.summarizeQwenRealtime=(v:any)=>({configured:!!v})
 runInNewContext(handlerText(channel),f.context);const mutation=f.context.handler({sender:f.sender},config)
 f.loaded.resolve(config);expect(await old).toHaveProperty('name','AbortError');expect(atFirstAwait).toBe(true);expect(f.fetchImpl).not.toHaveBeenCalled()
 held.resolve(channel.includes('save')?config.apiKey:undefined);await mutation;f.cleanup()
})
it('actual runtime:restart cancels old settings wait before reload reads and preserves original applied ready result',async()=>{
 const f=fixture();const old=f.run().catch((e:any)=>e);await f.entered.promise
 f.context.process={env:{}};f.context.app={getPath:()=> 'synthetic-not-read'};f.context.cloudProviderSettingsFingerprint=vi.fn(async()=> 'opaque-unchanged')
 f.context.modelSettings={load:vi.fn(async()=>null)};f.context.asrSettings={load:vi.fn(async()=>null)};f.context.qwenRealtimeSettings.load=vi.fn(async()=>config);f.context.voiceSettings={loadEffective:vi.fn(async()=>null)}
 const token={child:'synthetic-fresh',epoch:2,revision:1};f.context.localService={admitCloudProviderRefresh:vi.fn(()=>({syntheticAdmission:true})),restartCloudProviderSnapshot:vi.fn(async()=>token),cloudProviderSnapshotApplied:vi.fn(t=>t===token),getRuntime:()=>({status:'ready',synthetic:true})}
 runInNewContext(handlerText('runtime:restart'),f.context);const reload=f.context.handler({sender:f.sender})
 f.loaded.resolve(config);expect(await old).toHaveProperty('name','AbortError');expect(f.fetchImpl).not.toHaveBeenCalled();expect(await reload).toEqual({status:'ready',synthetic:true})
 expect(f.context.localService.cloudProviderSnapshotApplied).toHaveBeenCalledWith(token);f.cleanup()
})
it('actual negotiate is rejected while reload pending without reading configuration',async()=>{
 const f=fixture(),held=deferred<void>();const pending=f.provider.reload({...dependencies(),loadSnapshot:async()=>{await held.promise;return{modelConfig:null,asrConfig:null,voiceConfig:null}}})
 await expect(f.run()).rejects.toHaveProperty('name','AbortError');expect(f.context.qwenRealtimeSettings.load).not.toHaveBeenCalled()
 held.resolve();await pending;f.cleanup()
})
it('owner cancellation dominates a late settings failure and remains a safe AbortError',async()=>{
 const f=fixture(),entered=deferred<void>();let reject!:(e:any)=>void
 f.context.qwenRealtimeSettings.load=vi.fn(()=>{entered.resolve();return new Promise((_r,r)=>{reject=r})})
 const result=f.run().catch((e:any)=>e);await entered.promise;f.sender.emit('render-process-gone',{});reject(Error('synthetic-settings-failed'))
 expect(await result).toMatchObject({name:'AbortError',message:'语音连接已取消，请重新连接。'});expect(f.fetchImpl).not.toHaveBeenCalled();f.cleanup()
})
it('current settings failure stays unchanged and releases the request/controller/listeners',async()=>{
 const f=fixture();f.context.qwenRealtimeSettings.load=vi.fn(async()=>{throw Error('synthetic-settings-failed')})
 await expect(f.run()).rejects.toThrow('synthetic-settings-failed');expect((f.provider as any).providerRequests.size).toBe(0);f.cleanup()
})


it.each(['bounded_segmented','blocked'])('actual negotiate refuses %s policy before any provider exchange',async(capture)=>{
 const f=fixture();f.context.readVoiceRuntimePolicy.mockResolvedValue({version:1,capture,runtimeEpoch:1});f.loaded.resolve(config)
 await expect(f.run()).rejects.toThrow('音频云端许可');expect(f.exchange).not.toHaveBeenCalled();expect(f.fetchImpl).not.toHaveBeenCalled()
 expect(f.context.readVoiceRuntimePolicy).toHaveBeenCalledExactlyOnceWith({sender:f.sender});f.cleanup()
})
it('actual negotiate refuses an unavailable fresh policy without provider HTTP',async()=>{
 const f=fixture();f.context.readVoiceRuntimePolicy.mockRejectedValue(Error('VOICE_RUNTIME_POLICY_UNAVAILABLE'));f.loaded.resolve(config)
 await expect(f.run()).rejects.toThrow('VOICE_RUNTIME_POLICY_UNAVAILABLE');expect(f.exchange).not.toHaveBeenCalled();expect(f.fetchImpl).not.toHaveBeenCalled();f.cleanup()
})
it('actual negotiate rechecks current permission after exchange before returning the answer',async()=>{
 const f=fixture();f.context.readVoiceRuntimePolicy.mockResolvedValueOnce({version:1,capture:'configured',runtimeEpoch:1}).mockResolvedValueOnce({version:1,capture:'bounded_segmented',runtimeEpoch:1});f.loaded.resolve(config)
 await expect(f.run()).rejects.toThrow('音频许可或本机会话已变化');expect(f.exchange).toHaveBeenCalledOnce();expect(f.fetchImpl).toHaveBeenCalledOnce()
 expect(f.context.readVoiceRuntimePolicy).toHaveBeenNthCalledWith(1,{sender:f.sender});expect(f.context.readVoiceRuntimePolicy).toHaveBeenNthCalledWith(2,{sender:f.sender})
 expect(f.context.diagnosticRecorder.record.mock.calls.map((c:any)=>c[0].event)).not.toContain('voice.realtime.signaling_succeeded');f.cleanup()
})
it('actual negotiate refuses a new backend epoch even if the later policy is configured',async()=>{
 const f=fixture();f.context.readVoiceRuntimePolicy.mockResolvedValueOnce({version:1,capture:'configured',runtimeEpoch:1}).mockResolvedValueOnce({version:1,capture:'configured',runtimeEpoch:2});f.loaded.resolve(config)
 await expect(f.run()).rejects.toThrow('音频许可或本机会话已变化');expect(f.exchange).toHaveBeenCalledOnce();f.cleanup()
})
it('provider retirement during the initial policy read prevents exchange',async()=>{
 const f=fixture(),policy=deferred<any>(),entered=deferred<void>();f.context.readVoiceRuntimePolicy.mockImplementationOnce(()=>{entered.resolve();return policy.promise});f.loaded.resolve(config)
 const result=f.run().catch((e:any)=>e);await entered.promise;await f.provider.mutate(async()=> 'retired')
 policy.resolve({version:1,capture:'configured',runtimeEpoch:1});expect(await result).toHaveProperty('name','AbortError');expect(f.exchange).not.toHaveBeenCalled();expect(f.fetchImpl).not.toHaveBeenCalled();f.cleanup()
})
it('sender retirement during the final policy read prevents a late answer from escaping',async()=>{
 const f=fixture(),policy=deferred<any>(),entered=deferred<void>();f.context.readVoiceRuntimePolicy.mockResolvedValueOnce({version:1,capture:'configured',runtimeEpoch:1}).mockImplementationOnce(()=>{entered.resolve();return policy.promise});f.loaded.resolve(config)
 const result=f.run().catch((e:any)=>e);await entered.promise;f.sender.emit('render-process-gone',{})
 policy.resolve({version:1,capture:'configured',runtimeEpoch:1});expect(await result).toHaveProperty('name','AbortError');expect(f.exchange).toHaveBeenCalledOnce();f.cleanup()
})
it('unchanged configured policy is read twice and allows the original exchange result',async()=>{
 const f=fixture();f.loaded.resolve(config);expect(await f.run()).toHaveProperty('answerSdp',answer)
 expect(f.context.readVoiceRuntimePolicy).toHaveBeenCalledTimes(2);expect(f.exchange).toHaveBeenCalledOnce();expect(f.fetchImpl).toHaveBeenCalledOnce();f.cleanup()
})
