import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { useEffect, useState } from 'react'
const leaf = vi.hoisted(() => ({ mounts: [] as string[], standbyMounts: 0, props: null as null | { initial: {session:{session_id:string;child_id:string}}; onBack:(active:boolean)=>void; onSwitchChild:(child:string,before:(output:unknown)=>Promise<void>,action:string)=>Promise<void> } }))
vi.mock('./LocalInputs', () => ({ HouseholdStandbyVoiceInput: () => { useEffect(()=>{leaf.standbyMounts++},[]); return null } }))
vi.mock('./SessionWorkspace', () => ({ SessionWorkspace: (props: NonNullable<typeof leaf.props>) => { leaf.props=props; const {initial}=props; useEffect(()=>{leaf.mounts.push(initial.session.session_id)},[]); return <div>学习区:{initial.session.session_id}:{initial.session.child_id}<button onClick={()=>props.onBack(true)}>返回首页</button></div> } }))
vi.mock('./ParentCenter', () => ({ ParentCenter: () => { const [draft,setDraft]=useState('');return <div>家长操作区<input aria-label="合成未提交草稿" value={draft} onChange={e=>setDraft(e.target.value)}/></div> } }))
vi.mock('./DeviceAvailabilityStrip', () => ({ DeviceAvailabilityStrip: () => null }))
import { App } from './App'
import { localApi, resetRuntimeCache } from './api'
import { prepareProfileSwitch } from './profileSwitchHandoff'
const old = { status: 'ready' as const, localApiUrl: 'http://127.0.0.1:19021', sessionToken: 'PUBLIC-A' }
const fresh = { status: 'ready' as const, localApiUrl: 'http://127.0.0.1:19022', sessionToken: 'PUBLIC-B' }
const a = { child_id: 'synthetic-a', nickname: '甲', grade: '四年级', curriculum_region: '江苏南京', status: 'active' }
const b = { ...a, child_id: 'synthetic-b', nickname: '乙' }
const original = { session_id: 'synthetic-original', child_id: a.child_id, status: 'active', observation_status: 'paused', observation_revision: 0, observation_generation: 'a'.repeat(64) }
const foreign = { ...original, session_id: 'synthetic-foreign', child_id: b.child_id, observation_generation: 'b'.repeat(64) }
const response = (value: unknown) => new Response(JSON.stringify(value), { status: 200, headers: { 'Content-Type': 'application/json' } })
beforeEach(() => { localStorage.clear(); resetRuntimeCache(); leaf.mounts = []; leaf.standbyMounts=0;leaf.props=null })
afterEach(() => { cleanup(); delete window.desktop; resetRuntimeCache(); vi.unstubAllGlobals(); vi.restoreAllMocks() })
async function fixture(replaceActive = false, holdWorkspace = false) {
  let releaseWorkspace!:()=>void, recoveryRead=false
  let current = old, ipcCalls = 0, openParent!: () => void, release!: (value: typeof fresh) => void
  const reads: string[] = []
  window.desktop = { getRuntimeConfig: vi.fn(() => {
    if (++ipcCalls === 2) return new Promise<typeof fresh>(resolve => { release = resolve })
    return Promise.resolve(current)
  }), onOpenParentCenter: (callback: () => void) => { openParent = callback; return () => {} } } as unknown as Window['desktop']
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const { pathname: path, port } = new URL(url)
    if (path === '/v1/parent/status') throw new TypeError('PUBLIC_SYNTHETIC_OFFLINE')
    if (path === '/v1/bootstrap') return response({ children: [a, b], active_session: replaceActive && port === '19022' ? foreign : original,
      devices: [], model: { configured: false }, privacy_paused: false })
    if (path === '/v1/install-setup/status') return response({})
    if(path === '/v1/sessions/'+original.session_id+'/switch-child')return response(foreign)
    if(path === '/v1/sessions/'+original.session_id+'/observation/pause')return response(original)
    if (path === '/v1/sessions/' + original.session_id) { reads.push(port + ':original'); if(port==='19022'&&holdWorkspace){recoveryRead=true;return new Promise<Response>(resolve=>{releaseWorkspace=()=>resolve(response({session:original}))})} return response({ session: original }) }
    if (path === '/v1/sessions/' + foreign.session_id) { reads.push(port + ':foreign'); return response({ session: foreign }) }
    throw new Error('Unexpected synthetic endpoint ' + path)
  }))
  render(<App />)
  await screen.findByText('学习区:' + original.session_id + ':' + a.child_id)
  const failure = localApi.parentStatus().catch(error => error)
  await waitFor(() => expect(ipcCalls).toBe(2))
  return { reads, recoveryRead:()=>recoveryRead, releaseWorkspace:()=>releaseWorkspace(), openParent: () => openParent(), finish: async () => {
    await act(async () => { current = fresh; release(fresh); await failure; await new Promise(resolve => setTimeout(resolve, 0)) })
  } }
}
it('keeps a newer explicit parent entry after the old identity IPC resolves', async () => {
  const f = await fixture()
  act(() => f.openParent())
  expect(screen.getByText('家长操作区')).toBeInTheDocument()
  await f.finish()
  expect(screen.queryByText('家长操作区')).toBeInTheDocument()
  expect(f.reads).toEqual(['19021:original'])
})
it('reconciles the exact original session and child even if bootstrap advertises another active child', async () => {
  const f = await fixture(true)
  await f.finish()
  expect(screen.queryByText('学习区:' + original.session_id + ':' + a.child_id)).toBeInTheDocument()
  expect(f.reads).toEqual(['19021:original', '19022:original'])
  expect(screen.queryByText('学习区:' + foreign.session_id + ':' + b.child_id)).not.toBeInTheDocument()
})
it('does not start a new automatic workspace read after a newer privacy-pause intent', async () => {
  const f = await fixture()
  act(() => window.dispatchEvent(new CustomEvent('hsc-privacy-pause')))
  await f.finish()
  expect(f.reads).toEqual(['19021:original'])
})
it('keeps the newer home destination without reentering the active workspace',async()=>{
 const f=await fixture();fireEvent.click(screen.getByRole('button',{name:'返回首页'}));await screen.findByText('继续今天的学习');await f.finish();expect(f.reads).toEqual(['19021:original']);expect(screen.queryByText('学习区:'+original.session_id+':'+a.child_id)).not.toBeInTheDocument()
})
it('a later parent entry also defeats a recovery workspace read already in flight',async()=>{
 const f=await fixture(false,true);await f.finish();await waitFor(()=>expect(f.recoveryRead()).toBe(true));act(()=>f.openParent());expect(screen.getByText('家长操作区')).toBeInTheDocument();await act(async()=>{f.releaseWorkspace();await Promise.resolve()});expect(screen.getByText('家长操作区')).toBeInTheDocument();expect(leaf.mounts).toEqual([original.session_id])
})
it('a user privacy stop during recovery keeps the placeholder instead of remounting input',async()=>{
 const f=await fixture(false,true);await f.finish();await waitFor(()=>expect(f.recoveryRead()).toBe(true));act(()=>window.dispatchEvent(new CustomEvent('hsc-privacy-pause')));await act(async()=>{f.releaseWorkspace();await Promise.resolve()});expect(screen.getByText('监听保持暂停，原学习记录已保留。')).toBeInTheDocument();expect(leaf.mounts).toEqual([original.session_id]);expect(screen.queryByRole('button',{name:'返回首页'})).not.toBeInTheDocument()
})
it('recovering the workspace neither mounts home standby nor emits a synthetic privacy stop',async()=>{
 const f=await fixture(false,true);const before=leaf.standbyMounts,privacy=vi.fn();window.addEventListener('hsc-privacy-pause',privacy)
 try{await f.finish();await waitFor(()=>expect(f.recoveryRead()).toBe(true));expect(leaf.standbyMounts).toBe(before);expect(privacy).not.toHaveBeenCalled();expect(localStorage.getItem('hsc-privacy-stop-v1:'+original.session_id)).toBeNull();await act(async()=>{f.releaseWorkspace();await Promise.resolve()});await screen.findByText('学习区:'+original.session_id+':'+a.child_id);expect(leaf.standbyMounts).toBe(before);expect(leaf.mounts).toEqual([original.session_id,original.session_id])}finally{window.removeEventListener('hsc-privacy-pause',privacy)}
})
it('automatic identity recovery preserves a newer parent local draft without unmounting it',async()=>{
 const f=await fixture();act(()=>f.openParent());fireEvent.change(screen.getByLabelText('合成未提交草稿'),{target:{value:'公开合成未提交内容'}});await f.finish();expect(screen.getByLabelText('合成未提交草稿')).toHaveValue('公开合成未提交内容');expect(f.reads).toEqual(['19021:original'])
})
it('a newer validated child switch owns recovery instead of the earlier child',async()=>{
 const f=await fixture();vi.spyOn(localApi,'completeAgentTurn').mockResolvedValue({agent_turn_id:'public-switch',tool_status:'completed'} as never)
 const handoff=prepareProfileSwitch({turnId:'public-switch',sourceSessionId:original.session_id,sourceChildId:a.child_id,targetChildId:b.child_id,clientActionId:'public-switch-action'})
 await act(async()=>{await leaf.props!.onSwitchChild(b.child_id,async()=>{},handoff.clientActionId)});expect(screen.getByText('学习区:'+foreign.session_id+':'+b.child_id)).toBeInTheDocument();await f.finish();expect(screen.getByText('学习区:'+foreign.session_id+':'+b.child_id)).toBeInTheDocument();expect(f.reads).toEqual(['19021:original','19021:foreign','19022:foreign'])
})
