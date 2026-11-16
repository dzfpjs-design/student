import { act,cleanup,fireEvent,render,screen,waitFor,within } from '@testing-library/react'
import { afterEach,expect,it,vi } from 'vitest'
const control=vi.hoisted(()=>({methods:{}as Record<string,(...args:any[])=>Promise<any>>}))
vi.mock('./api',async(load)=>{
  const actual=await load<typeof import('./api')>()
  const missing=async()=>{throw Error('unrelated parent panel outside navigation test')}
  return {...actual,requestParentLocal:(...args:unknown[])=>(control.methods.requestParentLocal??missing)(...args),localApi:new Proxy(actual.localApi,{get:(_target,key)=>control.methods[String(key)]??missing})}
})
import { ParentCenter } from './ParentCenter'
import { ApiError } from './api'
import { ParentTextbookDraftHost } from './parentTextbookDraftHost'
import type { ChildProfile,BootstrapState } from './types'

afterEach(()=>{cleanup();control.methods={};vi.restoreAllMocks()})
const children=[{child_id:'public-a',nickname:'合成甲',grade:'四年级',curriculum_region:'公开合成',status:'active'},
  {child_id:'public-b',nickname:'合成乙',grade:'初三',curriculum_region:'公开合成',status:'active'}]as ChildProfile[]
const model={configured:false,provider:'qwen',model:'public-unconfigured'}as BootstrapState['model']
const dashboard={today:{items:[]},children:[],privacy:{},health:{maintenance:{daily_backup:null,retention_cleanup:null},devices:[],device_incidents:[],deletion_tasks:[],
  recent_maintenance_failures:[],recent_model_failures:[],recent_report_failures:[],recent_agent_failures:[],recent_deletion_failures:[]},
  device_bindings:[],device_qualification:{recent_runs:[],required:[],profile_missing:{}},sensor_release_reviews:[],safety_events:[]}

it('actual ParentCenter child tabs and return keep the existing dirty editor mounted until saved, without queued navigation',async()=>{
  let offline=false,revision=0,saved:unknown=null,last:string|null=null,expected:number|null=null,draftWrites=0
  control.methods.parentStatus=async()=>({configured:true,locked_seconds:0})
  control.methods.parentUnlock=async()=>({})
  control.methods.parentDashboard=async()=>dashboard
  control.methods.knowledgeGraphRequest=async(path:string,init?:RequestInit)=>{
    if(path.includes('/drafts/')){
      if(offline)throw Error('synthetic offline')
      const child=path.includes('/public-b/')?'public-b':'public-a'
      if(init){draftWrites++;const body=JSON.parse(String(init.body));revision++;saved=body.payload;last=body.client_action_id;expected=body.expected_revision}
      return {draft:{child_id:child,scope:'capture',storage_generation:'navigation-generation-a',revision,payload:saved,last_action_id:last,
        last_expected_revision:expected,updated_at:null,stale_refs:[],cloud_transmission_allowed:false}}
    }
    return {items:[],nodes:[]}
  }
  const close=vi.fn();render(<ParentCenter draftHost={new ParentTextbookDraftHost(()=> 'memory-only')} children={children}model={model}onClose={close}/>)
  fireEvent.change(await screen.findByLabelText('家长 PIN'),{target:{value:'1234'}})
  fireEvent.click(screen.getByRole('button',{name:'解锁'}))
  await screen.findByRole('button',{name:'整理单题审核草稿'})
  await screen.findByText('草稿已从本机恢复。')
  expect(revision).toBe(0);expect(saved).toBeNull();expect(draftWrites).toBe(0)
  offline=true
  fireEvent.change(screen.getByLabelText('教材页标题'),{target:{value:'离线原稿不可丢'}})
  await screen.findByRole('button',{name:'重试草稿保存或恢复'})
  fireEvent.click(within(screen.getByRole('group',{name:'选择孩子'})).getByRole('button',{name:/合成乙/}))
  expect(screen.getByLabelText('教材页标题')).toHaveValue('离线原稿不可丢')
  await screen.findByText(/还有未确认保存的草稿，已留在当前孩子页面/)
  fireEvent.click(screen.getByTitle('返回孩子首页'));expect(close).not.toHaveBeenCalled()
  offline=false;fireEvent.click(screen.getByRole('button',{name:'重试草稿保存或恢复'}))
  await waitFor(()=>expect(saved).toMatchObject({page:{title:'离线原稿不可丢'}}))
  expect(close).not.toHaveBeenCalled()
  fireEvent.click(screen.getByTitle('返回孩子首页'));expect(close).toHaveBeenCalledOnce()
})

it('removes private editing immediately on expired PIN, retains it across unlock, and never sends while locked or before explicit recovery',async()=>{
 let offline=false,revision=0,saved:unknown=null,last:string|null=null,expected:number|null=null,expire=false
 const calls:{path:string;write:boolean}[]=[]
 control.methods.parentStatus=async()=>({configured:true,locked_seconds:0})
 control.methods.parentUnlock=async()=>({})
 control.methods.parentDashboard=async()=>dashboard
 control.methods.requestParentLocal=async()=>{if(expire)throw new ApiError('expired',401);throw Error('unrelated source unavailable')}
 control.methods.knowledgeGraphRequest=async(path:string,init?:RequestInit)=>{
  calls.push({path,write:!!init})
  if(path.includes('/drafts/')){
   if(offline)throw Error('synthetic offline')
   const child=path.includes('/public-b/')?'public-b':'public-a'
   if(init){const b=JSON.parse(String(init.body));revision++;saved=b.payload;last=b.client_action_id;expected=b.expected_revision}
   return{draft:{child_id:child,scope:'capture',storage_generation:'navigation-generation-a',revision,payload:saved,last_action_id:last,last_expected_revision:expected,updated_at:null,stale_refs:[],cloud_transmission_allowed:false}}
  }
  return{items:[],nodes:[]}
 }
 const host=new ParentTextbookDraftHost(()=>'memory-only')
 render(<ParentCenter draftHost={host} children={children}model={model}onClose={()=>undefined}/>)
 fireEvent.change(await screen.findByLabelText('家长 PIN'),{target:{value:'1234'}});fireEvent.click(screen.getByRole('button',{name:'解锁'}))
 await screen.findByText('草稿已从本机恢复。')
 expect(revision).toBe(0);expect(saved).toBeNull();expect(calls.filter(c=>c.path.includes('/drafts/')&&c.write)).toHaveLength(0)
 offline=true
 fireEvent.change(screen.getByLabelText('教材页标题'),{target:{value:'PIN锁前最后一个字🙂'}})
 await screen.findByRole('button',{name:'重试草稿保存或恢复'})
 expire=true;fireEvent.click(screen.getByRole('button',{name:'刷新待办'}))
 await screen.findByLabelText('家长 PIN');expect(screen.queryByLabelText('教材页标题')).not.toBeInTheDocument()
 expect(host.entry(control.methods.knowledgeGraphRequest,'public-a','capture').journal.state.payload).toMatchObject({page:{title:'PIN锁前最后一个字🙂'}})
 const lockedCalls=calls.length,lockedDraftCalls=calls.filter(c=>c.path.includes('/drafts/')).length;await new Promise(resolve=>setTimeout(resolve,20));expect(calls).toHaveLength(lockedCalls)
 offline=false;expire=false
 fireEvent.change(screen.getByLabelText('家长 PIN'),{target:{value:'1234'}});fireEvent.click(screen.getByRole('button',{name:'解锁'}))
 const restore=await screen.findByRole('button',{name:'核对并恢复本窗口草稿'})
 expect(screen.queryByDisplayValue('PIN锁前最后一个字🙂')).not.toBeInTheDocument();expect(calls.filter(c=>c.path.includes('/drafts/'))).toHaveLength(lockedDraftCalls)
 fireEvent.click(restore)
 await waitFor(()=>expect(screen.getByLabelText('教材页标题')).toHaveValue('PIN锁前最后一个字🙂'))
 expect(saved).not.toMatchObject({page:{title:'PIN锁前最后一个字🙂'}})
 fireEvent.click(screen.getByRole('button',{name:'重试草稿保存或恢复'}))
 await waitFor(()=>expect(saved).toMatchObject({page:{title:'PIN锁前最后一个字🙂'}}))
})

it('ignores a late old unlock dashboard failure after PIN is locked and successfully unlocked again',async()=>{
 let rejectOld!:(e:unknown)=>void,reads=0,statusReads=0
 control.methods.parentStatus=async()=>{statusReads++;return{configured:true,locked_seconds:0}}
 control.methods.parentUnlock=async()=>({})
 control.methods.parentDashboard=async()=>++reads===1?new Promise((_r,reject)=>{rejectOld=reject}):dashboard
 control.methods.knowledgeGraphRequest=async(path:string)=>path.includes('/drafts/')?{draft:{child_id:'public-a',scope:'capture',storage_generation:'navigation-generation-a',revision:0,payload:null,last_action_id:null,last_expected_revision:null,updated_at:null,stale_refs:[],cloud_transmission_allowed:false}}:{items:[],nodes:[]}
 render(<ParentCenter children={children}model={model}onClose={()=>undefined}/>)
 fireEvent.change(await screen.findByLabelText('家长 PIN'),{target:{value:'1234'}});fireEvent.click(screen.getByRole('button',{name:'解锁'}))
 await waitFor(()=>expect(rejectOld).toBeTypeOf('function'))
 const {changeParentDraftAuthority}=await import('./parentDraftAuthority')
 act(()=>changeParentDraftAuthority('locked'))
 fireEvent.change(screen.getByLabelText('家长 PIN'),{target:{value:'1234'}})
 expect(screen.getByRole('button',{name:'解锁'})).not.toBeDisabled();fireEvent.click(screen.getByRole('button',{name:'解锁'}))
 await screen.findByLabelText('教材页标题')
 await act(async()=>{rejectOld(new ApiError('old locked',423,'PARENT_LOCKED'));await Promise.resolve()})
 expect(screen.getByLabelText('教材页标题')).toBeInTheDocument();expect(screen.queryByText('old locked')).not.toBeInTheDocument();expect(statusReads).toBe(1)
})
