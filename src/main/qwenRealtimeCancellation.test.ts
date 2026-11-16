// @vitest-environment node
import { expect, it, vi } from 'vitest'
import { createServer } from 'node:http'
import { once } from 'node:events'
vi.mock('electron',()=>({safeStorage:{},app:{}}))
import { exchangeQwenRealtimeSdp, defaultQwenRealtimeConfig, validateQwenRealtimeConfig } from './qwen-realtime'
const config=defaultQwenRealtimeConfig('public-synthetic-stage-key','https://ws-test.cn-beijing.maas.aliyuncs.com')
const offer=`v=0\r\n${'a'.repeat(80)}\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n`
const answer=`v=0\r\n${'b'.repeat(80)}\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n`
function deferred<T>(){let resolve!:(v:T)=>void;const promise=new Promise<T>(r=>{resolve=r});return{promise,resolve}}
it('pre-aborted external signal prevents fetch and retains a safe AbortError',async()=>{
 const c=new AbortController();c.abort(new DOMException('语音连接已取消，请重新连接。','AbortError'));const request=vi.fn()
 await expect(exchangeQwenRealtimeSdp(config,offer,request as never,c.signal)).rejects.toMatchObject({name:'AbortError',message:'语音连接已取消，请重新连接。'});expect(request).not.toHaveBeenCalled()
})
it('actual composite signal cancels a mock fetch and retains original 20s timeout and redirect:error',async()=>{
 const original=AbortSignal.timeout;const timeout=vi.spyOn(AbortSignal,'timeout').mockImplementation(original)
 const c=new AbortController(),entered=deferred<void>();let observed!:AbortSignal
 const request=vi.fn(async(_u:any,init:any)=>{observed=init.signal;expect(init.redirect).toBe('error');entered.resolve();return new Promise<Response>((_r,reject)=>observed.addEventListener('abort',()=>reject(observed.reason),{once:true}))})
 try{const result=exchangeQwenRealtimeSdp(config,offer,request as typeof fetch,c.signal).catch(e=>e);await entered.promise;c.abort(new DOMException('语音连接已取消，请重新连接。','AbortError'));expect(await result).toHaveProperty('name','AbortError');expect(observed.aborted).toBe(true);expect(timeout).toHaveBeenCalledWith(20000)}finally{timeout.mockRestore()}
})
it('post-fetch abort rejects even a fetch implementation that ignores cancellation, before reading text',async()=>{
 const c=new AbortController(),pending=deferred<Response>();const text=vi.fn(async()=>answer)
 const result=exchangeQwenRealtimeSdp(config,offer,vi.fn(()=>pending.promise) as typeof fetch,c.signal).catch(e=>e)
 c.abort();pending.resolve({ok:true,text} as any);expect(await result).toHaveProperty('name','AbortError');expect(text).not.toHaveBeenCalled()
})
it('post-text abort rejects an implementation that ignores body cancellation',async()=>{
 const c=new AbortController(),pending=deferred<string>(),entered=deferred<void>()
 const result=exchangeQwenRealtimeSdp(config,offer,vi.fn(async()=>({ok:true,text:()=>{entered.resolve();return pending.promise}})) as any,c.signal).catch(e=>e)
 await entered.promise;c.abort();pending.resolve(answer);expect(await result).toHaveProperty('name','AbortError')
})
it('original timeout also rejects an ignoring-abort late fetch/body and never becomes successful',async()=>{
 const timer=new AbortController();const timeout=vi.spyOn(AbortSignal,'timeout').mockReturnValue(timer.signal);const pending=deferred<Response>()
 try{const result=exchangeQwenRealtimeSdp(config,offer,vi.fn(()=>pending.promise) as typeof fetch).catch(e=>e);timer.abort(new DOMException('The operation was aborted due to timeout','TimeoutError'));pending.resolve(new Response(answer));expect(await result).toHaveProperty('name','TimeoutError');expect(timeout).toHaveBeenCalledWith(20000)}finally{timeout.mockRestore()}
})
it('original 3arg normal contract and HTTPS/model/SDP guards are preserved',async()=>{
 const request=vi.fn(async(_url: Parameters<typeof fetch>[0], _init?: Parameters<typeof fetch>[1])=>new Response(answer));expect(await exchangeQwenRealtimeSdp(config,offer,request as typeof fetch)).toEqual({answerSdp:answer,model:config.model,voice:config.voice})
 expect(request.mock.calls[0][1]).toMatchObject({method:'POST',redirect:'error',body:offer})
 await expect(exchangeQwenRealtimeSdp({...config,enabled:false},offer,request as typeof fetch)).rejects.toThrow('停用')
 await expect(exchangeQwenRealtimeSdp(config,'invalid',request as typeof fetch)).rejects.toThrow('Offer SDP')
 expect(()=>validateQwenRealtimeConfig({...config,endpoint:'http://127.0.0.1'})).toThrow('HTTPS');expect(()=>validateQwenRealtimeConfig({...config,endpoint:'https://user:pass@ws-test.cn-beijing.maas.aliyuncs.com'})).toThrow('HTTPS')
 expect(request).toHaveBeenCalledOnce()
})
it('native fetch aborts on a bounded public-synthetic loopback request; it grants no remote physical-closure credit',async()=>{
 const received=deferred<void>();const server=createServer((request,response)=>{request.resume();received.resolve(); /* Deliberately hold an answer until abort. No secrets, cloud or device. */ })
 const listening=once(server,'listening');server.listen(0,'127.0.0.1');await listening
 const address=server.address();if(!address||typeof address==='string')throw Error('loopback fixture unavailable')
 const c=new AbortController()
 const adapter:typeof fetch=(_url,init)=>fetch(`http://127.0.0.1:${address.port}/public-synthetic-sdp`,{...init,headers:{'Content-Type':'application/sdp'}})
 try{const result=exchangeQwenRealtimeSdp(config,offer,adapter,c.signal).catch(e=>e);await received.promise;c.abort(new DOMException('语音连接已取消，请重新连接。','AbortError'));expect(await result).toHaveProperty('name','AbortError')}
 finally{server.closeAllConnections();await new Promise<void>((resolve,reject)=>server.close(error=>error?reject(error):resolve()))}
})
