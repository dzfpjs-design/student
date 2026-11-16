import { afterEach, expect, it, vi } from 'vitest'
import { preparePendingControlledEnd as prepare, readPendingControlledEnds as read,
  markControlledEndClosed as closed, finishPendingControlledEnd as finish, clearPendingControlledEnds as clear,
  runPendingControlledEnd as run, type ControlledEndScope, type PendingControlledEnd } from './pendingControlledEnd'

afterEach(() => { localStorage.clear(); vi.restoreAllMocks() })
const scope = (): ControlledEndScope => ({childId:'a',sessionId:'s',kind:'game',activityId:crypto.randomUUID()})
const create = (s=scope()): PendingControlledEnd => prepare(s,{reason:'child_requested',actualSeconds:12})
const receipt = (p:PendingControlledEnd) => ({child_id:p.childId,session_id:p.sessionId,
  game_session_id:p.activityId,status:'stopped',device_closed:true as const,task_resumed:false as const})

it('stores multiple exact requests, scopes reads, and freezes first request payload across retry',()=>{
  const first=create(), second=create(), other=create({...scope(),childId:'b'}), next=create({...scope(),sessionId:'next'})
  expect(read('s','a','game')).toEqual([first,second])
  expect(read('s','b','game')).toEqual([other])
  expect(read('next','a','game')).toEqual([next])
  expect(read('s','a','video')).toEqual([])
  expect(prepare(first,{reason:'renderer_reloaded',actualSeconds:0})).toEqual(first)
  expect(JSON.stringify(first)).not.toMatch(/path|process|question|transcript/)
})
it('finishes only a matched closed request and retains other records',()=>{
  const first=create(), second=create()
  expect(()=>finish(first,receipt(first))).toThrow()
  expect(()=>closed(first,{closed:true,sessionId:second.activityId})).toThrow()
  const ended=closed(first,{closed:true,sessionId:first.activityId})
  expect(()=>finish(ended,{...receipt(ended),task_resumed:undefined} as never)).toThrow()
  expect(()=>finish(ended,{...receipt(ended),task_resumed:true} as never)).toThrow()
  expect(()=>finish(ended,receipt(second))).toThrow()
  expect(()=>finish({...ended,requestId:'different'},receipt(ended))).toThrow()
  finish(ended,receipt(ended))
  expect(read('s','a','game')).toEqual([second])
})
it.each(['null','[]','{bad','{"version":99}'])('bad scoped record %s remains visible as unavailable, not removed or selected elsewhere',raw=>{
  create()
  const key=Object.keys(localStorage)[0]
  localStorage.setItem(key,raw)
  const other=create({...scope(),childId:'b'})
  expect(()=>read('s','a','game')).toThrow()
  expect(localStorage.getItem(key)).toBe(raw)
  expect(read('s','b','game')).toEqual([other])
})
it('a silently dropped write is unavailable before any caller can close',()=>{
  vi.spyOn(Storage.prototype,'setItem').mockImplementation(()=>undefined)
  expect(()=>create()).toThrow()
})
it('failed removal retains closed request and never requires another close',()=>{
  const p=create(), ended=closed(p,{closed:true,sessionId:p.activityId})
  const remove=vi.spyOn(Storage.prototype,'removeItem').mockImplementation(()=>{throw new Error('disk')})
  expect(()=>finish(ended,receipt(ended))).toThrow()
  expect(read('s','a','game')[0]).toEqual(ended)
  remove.mockRestore()
  finish(ended,receipt(ended))
  expect(read('s','a','game')).toEqual([])
})
it('explicit child/session cleanup keeps another scope, invalidates outstanding callbacks and never re-creates a deleted request',async()=>{
  const p=create(), other=create({...scope(),childId:'b'}), next=create({...scope(),sessionId:'next'})
  clear('a','s')
  expect(()=>closed(p,{closed:true,sessionId:p.activityId})).toThrow()
  expect(()=>prepare(p,{reason:'workspace_closed',actualSeconds:0})).toThrow()
  await expect(run(p,async()=>receipt(p))).rejects.toThrow()
  expect(read('s','a','game')).toEqual([])
  expect(read('s','b','game')).toEqual([other])
  expect(read('next','a','game')).toEqual([next])
  clear()
  expect(read('s','b','game')).toEqual([])
  expect(read('next','a','game')).toEqual([])
})
it('concurrent same-process remount joins the original request and a failed run can later retry',async()=>{
  const p=create()
  let release!:()=>void
  const operation=vi.fn(async()=>{await new Promise<void>(resolve=>{release=resolve});throw new Error('lost')})
  const first=run(p,operation), second=run(p,operation)
  expect(second).toBe(first)
  await Promise.resolve()
  expect(operation).toHaveBeenCalledTimes(1)
  release()
  await expect(first).rejects.toThrow('lost')
  await expect(run(p,async()=>receipt(p))).resolves.toEqual(receipt(p))
})
