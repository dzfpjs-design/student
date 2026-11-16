import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
const bridge=vi.hoisted(()=>({api:null as unknown,invoke:vi.fn()}))
vi.mock('electron',()=>({app:{getPath:()=>''},contextBridge:{exposeInMainWorld:(_:string,api:unknown)=>{bridge.api=api}},ipcRenderer:{invoke:(...args:unknown[])=>bridge.invoke(...args)},
 safeStorage:{isEncryptionAvailable:()=>true,encryptString:(v:string)=>Buffer.from(v).reverse(),decryptString:(v:Buffer)=>Buffer.from(v).reverse().toString()}}))
import '../../preload/index'
import { createTutorExplanationHandler } from '../../main/tutor-explanation-handler'
import { createTutorExplanationAuthorizer } from '../../main/tutor-explanation-authorization'
import { buildLocalServiceChildEnvironment } from '../../main/local-service'
import { localApi, resetRuntimeCache } from './api'
import { TutorExplanationSettingsPanel } from './TutorExplanationSettingsPanel'

it('a verified parent migrates the old key through actual preload/handler/store and the next runtime uses that configuration',async()=>{
 const dir=mkdtempSync(join(tmpdir(),'hsc-tutor-ipc-'));const key='synthetic-previous-tutor-key';let env:NodeJS.ProcessEnv={};let authCalls=0
 const runtime={status:'ready',localApiUrl:'http://127.0.0.1:12345',sessionToken:'synthetic-local',runtimeEpoch:4}
 const authorize=createTutorExplanationAuthorizer({senderId:7,current:()=>true,runtime:()=>runtime,externalBackend:false,fetchImpl:async (_url,init)=>{
   authCalls++;const h=new Headers(init?.headers);return h.get('X-HSC-Parent')==='synthetic-parent' ? Response.json({authorized:true}) : new Response('',{status:401})
 }})
 const handler=createTutorExplanationHandler({userData:dir,authorize,apply:async config=>{
   env=buildLocalServiceChildEnvironment({HSC_TUTOR_EXPLANATION_BASE_URL:'https://attacker.example.test/v1'}, {sessionToken:'synthetic-local',dataEncryptionKey:'synthetic-key',dataDir:dir,databasePath:join(dir,'test.db'),localModelDir:dir,modelConfig:null,voiceConfig:null,tutorExplanationConfig:config})
 }})
 try{
  writeFileSync(join(dir,'tutor-explanation-settings.json'),JSON.stringify({version:1,encryptedApiKey:Buffer.from(key).reverse().toString('base64')}))
  bridge.invoke.mockImplementation(async(channel:string,...args:unknown[])=>{
   if(channel==='tutor-explanation:settings') return handler(7,args[0] as string,args[1] as import('../../main/tutor-explanation-handler').TutorExplanationCommand)
   if(channel==='runtime:get-config') return runtime
   throw Error('UNEXPECTED_IPC_CHANNEL')
  })
  window.desktop=bridge.api as NonNullable<Window['desktop']>
  vi.stubGlobal('fetch',vi.fn(async(url:string)=>{if(!url.endsWith('/v1/parent/unlock'))throw Error('NETWORK_BLOCKED');return Response.json({parent_token:'synthetic-parent'})}))
  await localApi.parentUnlock('2468',false)
  render(<TutorExplanationSettingsPanel />)
  await screen.findByText('已保留原来的密钥，请补充服务地址后启用。')
  fireEvent.change(screen.getByLabelText('讲题服务地址'),{target:{value:'https://tutor.example.test/v1'}})
  fireEvent.click(screen.getByRole('checkbox'));fireEvent.click(screen.getByRole('button',{name:'保存讲题服务'}))
  await waitFor(()=>expect(env.HSC_TUTOR_EXPLANATION_BASE_URL).toBe('https://tutor.example.test/v1'))
  expect(env.HSC_TUTOR_EXPLANATION_API_KEY).toBe(key);expect(env.HSC_TUTOR_EXPLANATION_MODEL).toBe('deepseek-v4.1-flash')
  expect(readFileSync(join(dir,'tutor-explanation-settings.json'),'utf8')).not.toContain(key)
  expect(authCalls).toBeGreaterThanOrEqual(4)
  cleanup();render(<TutorExplanationSettingsPanel />)
  await waitFor(()=>expect(screen.getByLabelText('讲题服务地址')).toHaveValue('https://tutor.example.test/v1'))
 }finally{cleanup();resetRuntimeCache();vi.unstubAllGlobals();bridge.invoke.mockReset();delete window.desktop;rmSync(dir,{recursive:true,force:true})}
})

it('successful save shows a restart receipt and retires old parent authority before another action',async()=>{
 const dir=mkdtempSync(join(tmpdir(),'hsc-tutor-restart-ipc-'));const key='synthetic-previous-tutor-key'
 let runtime={status:'ready',localApiUrl:'http://127.0.0.1:12345',sessionToken:'synthetic-local-old',runtimeEpoch:4}
 const notices:string[]=[];const listen=(event:Event)=>{const detail=(event as CustomEvent).detail; if(detail?.notice)notices.push(detail.notice)}
 window.addEventListener('hsc-runtime-restarted',listen)
 const authorize=createTutorExplanationAuthorizer({senderId:7,current:()=>true,runtime:()=>runtime,externalBackend:false,fetchImpl:async()=>Response.json({authorized:true})})
 const handler=createTutorExplanationHandler({userData:dir,authorize,apply:async()=>{
   runtime={status:'ready',localApiUrl:'http://127.0.0.1:12346',sessionToken:'synthetic-local-new',runtimeEpoch:5};return 5
 }})
 try {
  writeFileSync(join(dir,'tutor-explanation-settings.json'),JSON.stringify({version:1,encryptedApiKey:Buffer.from(key).reverse().toString('base64')}))
  bridge.invoke.mockImplementation(async(channel:string,...args:unknown[])=>{
   if(channel==='tutor-explanation:settings')return handler(7,args[0] as string,args[1] as import('../../main/tutor-explanation-handler').TutorExplanationCommand)
   if(channel==='runtime:get-config')return runtime
   throw Error('UNEXPECTED_IPC_CHANNEL')
  })
  window.desktop=bridge.api as NonNullable<Window['desktop']>
  vi.stubGlobal('fetch',vi.fn(async()=>Response.json({parent_token:'synthetic-parent'})))
  await localApi.parentUnlock('2468',false)
  render(<TutorExplanationSettingsPanel />)
  await screen.findByText('已保留原来的密钥，请补充服务地址后启用。')
  fireEvent.change(screen.getByLabelText('讲题服务地址'),{target:{value:'https://tutor.example.test'}})
  fireEvent.click(screen.getByRole('checkbox'));fireEvent.click(screen.getByRole('button',{name:'保存讲题服务'}))
  await waitFor(()=>expect(notices).toEqual([expect.stringContaining('已保存并应用')]))
  expect(screen.getByText('讲题服务配置已保存。')).toBeInTheDocument()
  cleanup();render(<TutorExplanationSettingsPanel />)
  await screen.findByText('未能读取讲题服务配置，请重新解锁家长中心后重试。')
 } finally {window.removeEventListener('hsc-runtime-restarted',listen);cleanup();resetRuntimeCache();vi.unstubAllGlobals();bridge.invoke.mockReset();delete window.desktop;rmSync(dir,{recursive:true,force:true})}
})
