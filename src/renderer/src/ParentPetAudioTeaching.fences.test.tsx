import {act,cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {ParentPetAudioTeaching} from './ParentPetAudioTeaching'
import {parentPetPendingPrefix} from './petMaterialSupplyTypes'
const key=parentPetPendingPrefix('a')+'original-audio-purpose'
const newer=JSON.stringify({childId:'a',kind:'grant',body:{action_id:'concurrent-new',allowed:false}})
beforeEach(()=>localStorage.clear())
afterEach(()=>{cleanup();vi.restoreAllMocks()})
async function setup(hook?:(body:any)=>Promise<void>){
 const request=vi.fn(async(path:string,init?:RequestInit):Promise<any>=>{
  if(init?.method==='POST'){const body=JSON.parse(String(init.body));await hook?.(body);return{child_id:'a',grant:{...body,revision:1}}}
  return path.endsWith('/authorization')?{child_id:'a',configured:true,current:false,grant:null}:{child_id:'a',items:[]}
 })
 render(<ParentPetAudioTeaching childId="a" request={request}/>)
 await screen.findByText('此用途尚未获得当前许可')
 fireEvent.click(screen.getByLabelText('允许原音练习建议'))
 return request
}
it('cannot POST after silently dropped journal write',async()=>{
 const request=await setup()
 vi.spyOn(Storage.prototype,'setItem').mockImplementation(()=>{})
 fireEvent.click(screen.getByText('保存授权'))
 await waitFor(()=>expect(screen.getByRole('status').textContent).toBeTruthy())
 expect(request.mock.calls.filter(([,init])=>init?.method==='POST')).toHaveLength(0)
})
it('cannot overwrite an unseen concurrent journal before POST',async()=>{
 const request=await setup()
 localStorage.setItem(key,newer)
 fireEvent.click(screen.getByText('保存授权'))
 await waitFor(()=>expect(screen.getByRole('status').textContent).toBeTruthy())
 expect(request.mock.calls.filter(([,init])=>init?.method==='POST')).toHaveLength(0)
 expect(localStorage.getItem(key)).toBe(newer)
})
it('cannot delete a newer journal when old POST receipt arrives',async()=>{
 let release!:()=>void
 const gate=new Promise<void>(r=>{release=r})
 const request=await setup(()=>gate)
 fireEvent.click(screen.getByText('保存授权'))
 await waitFor(()=>expect(request.mock.calls.some(([,init])=>init?.method==='POST')).toBe(true))
 localStorage.setItem(key,newer)
 await act(async()=>{release();await gate})
 await waitFor(()=>expect(screen.getByRole('status').textContent).toBeTruthy())
 expect(localStorage.getItem(key)).toBe(newer)
})
it.each(['hsc-privacy-pause','hsc-runtime-restarted'])('fences %s during write/readback before POST',async event=>{
 const request=await setup(),write=Storage.prototype.setItem
 vi.spyOn(Storage.prototype,'setItem').mockImplementation(function(this:Storage,k,v){write.call(this,k,v);window.dispatchEvent(new Event(event))})
 fireEvent.click(screen.getByText('保存授权'))
 await waitFor(()=>expect(screen.getByRole('status').textContent).toBeTruthy())
 expect(request.mock.calls.filter(([,init])=>init?.method==='POST')).toHaveLength(0)
})
it.each(['hsc-privacy-pause','hsc-runtime-restarted'])('keeps original intent after %s during POST response',async event=>{
 const request=await setup(async()=>{window.dispatchEvent(new Event(event))})
 fireEvent.click(screen.getByText('保存授权'))
 await waitFor(()=>expect(request.mock.calls.some(([,init])=>init?.method==='POST')).toBe(true))
 await act(async()=>{})
 expect(localStorage.getItem(key)).not.toBeNull()
})
it.each(['hsc-privacy-pause','hsc-runtime-restarted'])('does not restore success or private work after %s during refresh',async event=>{
 let saved=false,release!:()=>void
 const gate=new Promise<void>(r=>{release=r})
 const request=vi.fn(async(path:string,init?:RequestInit):Promise<any>=>{
  if(init?.method==='POST'){saved=true;return{child_id:'a',grant:{...JSON.parse(String(init.body)),revision:1}}}
  if(saved)await gate
  return path.endsWith('/authorization')?{child_id:'a',configured:true,current:false,grant:null}:{child_id:'a',items:[{child_id:'a',work_id:'work',question:'private original question',state:'delivered',generation:0}]}
 })
 render(<ParentPetAudioTeaching childId="a" request={request}/>);await screen.findByText('private original question')
 fireEvent.click(screen.getByLabelText('允许原音练习建议'));fireEvent.click(screen.getByText('保存授权'))
 await waitFor(()=>expect(saved).toBe(true))
 await act(async()=>{window.dispatchEvent(new Event(event));release();await gate})
 expect(screen.queryByText('private original question')).toBeNull()
 expect(screen.queryByText('操作已保存。')).toBeNull()
 expect(screen.getByRole('status').textContent).toContain('状态已变化')
})
