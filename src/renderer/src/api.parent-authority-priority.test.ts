import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {localApi,requestParentPetAudioPurpose,resetRuntimeCache} from './api'
import {changeParentDraftAuthority} from './parentDraftAuthority'
const path='/v1/parent/children/a/pet-audio/authorization'
const config={status:'ready',localApiUrl:'http://127.0.0.1:8765',sessionToken:'public-synthetic'}
beforeEach(()=>{resetRuntimeCache();window.desktop={getRuntimeConfig:async()=>config}as any})
afterEach(()=>{delete window.desktop;resetRuntimeCache();vi.unstubAllGlobals()})
it.each(['fetch','JSON']as const)('preserves original rejection priority after delayed %s without accepting or replaying old receipts',async stage=>{
 for(const kind of ['runtime','PIN','privacy','signal','runtime-and-privacy']as const){
  resetRuntimeCache();window.desktop={getRuntimeConfig:async()=>config}as any
  let release!:(value:any)=>void,jsonReads=0;const controller=new AbortController(),cancel=vi.fn(async()=>undefined)
  const fetcher=vi.fn(async(url:string)=>{
   if(String(url).endsWith('/unlock'))return{ok:true,json:async()=>({parent_token:'public-'+kind})}
   const response={ok:true,body:{cancel},json:()=>{jsonReads++;return stage==='JSON'?new Promise(r=>{release=r}):Promise.resolve({child_id:'a',grant:{action_id:'original'}})}}
   return stage==='fetch'?new Promise(r=>{release=()=>r(response)}):response
  });vi.stubGlobal('fetch',fetcher)
  await localApi.parentUnlock('1234',false)
  const pending=requestParentPetAudioPurpose(path,{method:'POST',body:'{}',signal:controller.signal})
  const rejected=expect(pending).rejects.toMatchObject(kind==='runtime'?{code:'LOCAL_SERVICE_CHANGED'}:kind==='PIN'?{code:'PARENT_SESSION_REQUIRED'}:{name:'AbortError'})
  await vi.waitFor(()=>expect(release).toBeTypeOf('function'))
  if(kind==='runtime'||kind==='runtime-and-privacy')resetRuntimeCache()
  if(kind==='PIN')changeParentDraftAuthority('locked')
  if(kind==='privacy'||kind==='runtime-and-privacy')window.dispatchEvent(new Event('hsc-privacy-pause'))
  if(kind==='signal')controller.abort()
  release({child_id:'a',grant:{action_id:'original'}});await rejected
  expect(fetcher).toHaveBeenCalledTimes(2)
  if(stage==='fetch'&&(kind==='runtime'||kind==='PIN'||kind==='runtime-and-privacy')){expect(jsonReads).toBe(0);expect(cancel).toHaveBeenCalledOnce()}
 }
})
