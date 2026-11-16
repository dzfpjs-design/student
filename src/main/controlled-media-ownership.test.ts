// @vitest-environment node
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { spawn, type ChildProcess } from 'node:child_process'
import { expect, it, vi } from 'vitest'
import { DatabaseSync } from 'node:sqlite'
import { queryProcessIdentity } from './game-process-identity'
import { ControlledMediaOwnership } from './controlled-media-ownership'
const waitExit=(p:ChildProcess)=>new Promise<void>(resolve=>{if(p.exitCode!==null||p.signalCode!==null)resolve();else p.once('exit',()=>resolve())})
it('owner-main death becomes an exact durable old-window proof without signaling a reused PID',async()=>{
  const root=await mkdtemp(join(tmpdir(),'hsc-media-owner-'))
  const child=spawn(process.execPath,['-e','setInterval(()=>{},1000)'],{stdio:'ignore'})
  try{
    const owner=await queryProcessIdentity(child.pid!)
    expect(owner).not.toBeNull()
    const original=new ControlledMediaOwnership(root,()=>Promise.resolve(owner!))
    original.opened(await original.reserve('original'))
    const recovered=new ControlledMediaOwnership(root)
    expect(await recovered.recover()).toEqual({unresolved:1})
    expect(await recovered.receipt('original')).toMatchObject({closed:false,sessionId:'original'})
    await expect(recovered.reserve('new')).rejects.toThrow('尚未确认关闭')
    child.kill('SIGKILL');await waitExit(child)
    expect(await recovered.recover()).toEqual({unresolved:0})
    expect(await new ControlledMediaOwnership(root).receipt('original')).toEqual({closed:true,sessionId:'original',proof:'observed_closed'})
    await expect(recovered.reserve('original')).rejects.toThrow('重复')
    recovered.closed(await recovered.reserve('new'),'cancelled_before_create')
  }finally{if(child.exitCode===null&&child.signalCode===null){child.kill('SIGKILL');await waitExit(child)}await rm(root,{recursive:true,force:true})}
}, process.platform === 'win32' ? 20_000 : 5_000)
it('unknown query, same-birth image change and another session cannot manufacture closure',async()=>{
  const root=await mkdtemp(join(tmpdir(),'hsc-media-owner-')),owner=(await queryProcessIdentity(process.pid))!
  try{
    const original=new ControlledMediaOwnership(root)
    const record=await original.reserve('current');original.opened(record)
    const query=vi.fn().mockRejectedValue(Error('ACCESS_DENIED'))
    const uncertain=new ControlledMediaOwnership(root,()=>Promise.resolve(owner),query)
    expect(await uncertain.recover()).toEqual({unresolved:1})
    expect(await uncertain.receipt('other')).toEqual({closed:false,sessionId:'other',proof:'unknown'})
    query.mockResolvedValue({...owner,executable:{...owner.executable,pathHash:'b'.repeat(64)}})
    expect(await uncertain.recover()).toEqual({unresolved:1})
    original.closed(original.read('current')!,'observed_closed')
    expect(await uncertain.receipt('current')).toMatchObject({closed:true})
  }finally{await rm(root,{recursive:true,force:true})}
})
it('a reused PID with a different birth proves only old owner death, and stale mutations cannot revive it',async()=>{
  const root=await mkdtemp(join(tmpdir(),'hsc-media-owner-')),owner=(await queryProcessIdentity(process.pid))!
  try{
    const original=new ControlledMediaOwnership(root),record=await original.reserve('original')
    const query=vi.fn().mockResolvedValue({...owner,birth:owner.birth+'-different'})
    const recovered=new ControlledMediaOwnership(root,()=>Promise.resolve(owner),query)
    expect(await recovered.recover()).toEqual({unresolved:0})
    expect(recovered.read('original')?.exitProof).toBe('owner_exit')
    expect(()=>original.opened(record)).toThrow('CONFLICT')
    expect(query).toHaveBeenCalledWith(owner.pid)
  }finally{await rm(root,{recursive:true,force:true})}
})
it('failed durable close rolls back; explicit same-instance retry persists, without accepting an invented source',async()=>{
  const root=await mkdtemp(join(tmpdir(),'hsc-media-owner-'))
  try{
    const owner=new ControlledMediaOwnership(root),record=owner.opened(await owner.reserve('original'))
    const c=new DatabaseSync(join(root,'ownership.sqlite'))
    try{
      c.exec("CREATE TRIGGER fail_close BEFORE UPDATE ON media_owner WHEN NEW.state='closed' BEGIN SELECT RAISE(ABORT,'INJECT_CLOSE_FAILURE'); END")
      expect(()=>owner.closed(record,'observed_closed')).toThrow('INJECT_CLOSE_FAILURE')
      expect(owner.read('original')?.state).toBe('open')
      expect(await new ControlledMediaOwnership(root).receipt('original')).toMatchObject({closed:false})
      await expect(owner.reserve('replacement')).rejects.toThrow('尚未确认关闭')
      c.exec('DROP TRIGGER fail_close')
      owner.closed(record,'observed_closed')
      expect(await new ControlledMediaOwnership(root).receipt('original')).toMatchObject({closed:true,sessionId:'original'})
      expect(()=>owner.closed({...record,instanceId:'wrong-instance'},'observed_closed')).toThrow('CONFLICT')
    }finally{c.close()}
  }finally{await rm(root,{recursive:true,force:true})}
})
it('competing reservations persist one owner; invalid JSON stays unknown and blocks new launch',async()=>{
  const root=await mkdtemp(join(tmpdir(),'hsc-media-owner-'))
  try{
    const a=new ControlledMediaOwnership(root),b=new ControlledMediaOwnership(root)
    const results=await Promise.allSettled([a.reserve('a'),b.reserve('b')])
    expect(results.filter(r=>r.status==='fulfilled')).toHaveLength(1)
    const c=new DatabaseSync(join(root,'ownership.sqlite'))
    try{c.exec("UPDATE media_owner SET owner_json='{broken'") }finally{c.close()}
    expect(await a.recover()).toEqual({unresolved:1})
    expect(await b.receipt('a')).toMatchObject({closed:false})
    await expect(b.reserve('other')).rejects.toThrow('尚未确认关闭')
  }finally{await rm(root,{recursive:true,force:true})}
})
