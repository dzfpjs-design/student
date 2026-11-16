import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {requestParentPetAudioPurpose,resetRuntimeCache} from './api'
const path='/v1/parent/children/a/pet-audio/authorization'
const config={status:'ready',localApiUrl:'http://127.0.0.1:8765',sessionToken:'synthetic'}
beforeEach(()=>resetRuntimeCache())
afterEach(()=>{delete window.desktop;resetRuntimeCache();vi.unstubAllGlobals()})
it.each(['privacy','runtime'])('does not POST after %s changes during runtime lookup',async kind=>{
 let release!:(value:unknown)=>void
 window.desktop={getRuntimeConfig:()=>new Promise(r=>{release=r})} as any
 const fetcher=vi.fn();vi.stubGlobal('fetch',fetcher)
 const result=requestParentPetAudioPurpose(path,{method:'POST',body:'{}'})
 const rejected=expect(result).rejects.toMatchObject(kind==='privacy'?{name:'AbortError'}:{code:'LOCAL_SERVICE_CHANGED'})
 if(kind==='privacy')window.dispatchEvent(new Event('hsc-privacy-pause'));else resetRuntimeCache()
 release(config);await rejected
 expect(fetcher).not.toHaveBeenCalled()
})
it.each(['privacy','runtime'])('does not accept a receipt after %s changes during JSON read',async kind=>{
 let release!:(value:unknown)=>void
 window.desktop={getRuntimeConfig:async()=>config} as any
 const fetcher=vi.fn(async()=>({ok:true,json:()=>new Promise(r=>{release=r})}))
 vi.stubGlobal('fetch',fetcher)
 const result=requestParentPetAudioPurpose(path,{method:'POST',body:'{}'})
 const rejected=expect(result).rejects.toMatchObject(kind==='privacy'?{name:'AbortError'}:{code:'LOCAL_SERVICE_CHANGED'})
 await vi.waitFor(()=>expect(release).toBeTypeOf('function'))
 if(kind==='privacy')window.dispatchEvent(new Event('hsc-privacy-pause'));else resetRuntimeCache()
 release({child_id:'a',grant:{action_id:'same'}});await rejected
 expect(fetcher).toHaveBeenCalledTimes(1)
})
it('cannot opt unrelated parent paths into the new helper',async()=>{
 const fetcher=vi.fn();vi.stubGlobal('fetch',fetcher)
 await expect(requestParentPetAudioPurpose('/v1/parent/report-deliveries',{method:'POST'})).rejects.toMatchObject({code:'PARENT_PATH_INVALID'})
 expect(fetcher).not.toHaveBeenCalled()
})
