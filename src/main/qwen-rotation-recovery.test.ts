import { expect, it, vi } from 'vitest'
import { QwenRotationRecovery } from './qwen-rotation-recovery'

function setup() {
  let binding='pending-one',owner=true
  const runtime={status:'ready',rotationAuthOnly:true,runtimeEpoch:1,localApiUrl:'http://127.0.0.1:1',sessionToken:'synthetic-session'}
  const dep={runtime:()=>runtime,binding:async()=>binding,authorize:vi.fn(async()=>true),stopAuth:vi.fn(async()=>{}),startOld:vi.fn(async(_current:()=>boolean)=>{}),restoreAuth:vi.fn(async()=>{}),cancelStarting:vi.fn(async()=>{})}
  return {dep,runtime,recovery:new QwenRotationRecovery(dep),owner:()=>owner,changeBinding:()=>{binding='changed'},loseOwner:()=>{owner=false}}
}
it('allows exactly one verified parent decision and never repeats the old-service start',async()=>{
  const f=setup();await f.recovery.arm();await f.recovery.continueWithOldKey('synthetic-parent-token',f.owner)
  expect(f.dep.stopAuth).toHaveBeenCalledOnce();expect(f.dep.startOld).toHaveBeenCalledOnce()
  await expect(f.recovery.continueWithOldKey('synthetic-parent-token',f.owner)).rejects.toThrow('ROTATION_RECOVERY_UNAVAILABLE')
})
it.each(['binding','owner','epoch','denied'] as const)('rejects %s changes during parent verification',async(reason)=>{
  const f=setup();await f.recovery.arm()
  f.dep.authorize.mockImplementation(async()=>{
    if(reason==='binding')f.changeBinding()
    if(reason==='owner')f.loseOwner()
    if(reason==='epoch')f.runtime.runtimeEpoch++
    return reason!=='denied'
  })
  await expect(f.recovery.continueWithOldKey('synthetic-parent-token',f.owner)).rejects.toThrow()
  expect(f.dep.stopAuth).not.toHaveBeenCalled();expect(f.dep.startOld).not.toHaveBeenCalled()
})
it('rejects concurrent confirmation',async()=>{
  const f=setup();await f.recovery.arm()
  let finish!: (authorized:boolean)=>void
  f.dep.authorize.mockImplementation(()=>new Promise(resolve=>{finish=resolve}))
  const first=f.recovery.continueWithOldKey('synthetic-parent-token',f.owner)
  await vi.waitFor(()=>expect(f.dep.authorize).toHaveBeenCalledOnce())
  await expect(f.recovery.continueWithOldKey('synthetic-parent-token',f.owner)).rejects.toThrow('ROTATION_RECOVERY_UNAVAILABLE')
  finish(true);await first;expect(f.dep.startOld).toHaveBeenCalledOnce()
})
it('requires a fresh PIN after the binding changes while the auth process stops',async()=>{
  const f=setup();await f.recovery.arm();f.dep.stopAuth.mockImplementation(async()=>f.changeBinding())
  await expect(f.recovery.continueWithOldKey('synthetic-parent-token',f.owner)).rejects.toThrow('ROTATION_RECOVERY_CHANGED')
  expect(f.dep.startOld).not.toHaveBeenCalled();expect(f.dep.restoreAuth).toHaveBeenCalledOnce()
})
it('restarts restricted authentication instead of retrying a failed old-service start',async()=>{
  const f=setup();await f.recovery.arm();f.dep.startOld.mockRejectedValueOnce(Error('synthetic-start-failure'))
  await expect(f.recovery.continueWithOldKey('synthetic-parent-token',f.owner)).rejects.toThrow('synthetic-start-failure')
  expect(f.dep.restoreAuth).toHaveBeenCalledOnce();expect(f.dep.startOld).toHaveBeenCalledOnce()
})
it('reconciles a partially failed PIN-process stop before allowing any future attempt',async()=>{
  const f=setup();await f.recovery.arm();f.dep.stopAuth.mockRejectedValueOnce(Error('synthetic-stop-unconfirmed'))
  f.dep.restoreAuth.mockRejectedValueOnce(Error('synthetic-child-still-open'))
  await expect(f.recovery.continueWithOldKey('synthetic-parent-token',f.owner)).rejects.toThrow('ROTATION_RECOVERY_RESTART_REQUIRED')
  expect(f.dep.restoreAuth).toHaveBeenCalledOnce();expect(f.dep.startOld).not.toHaveBeenCalled()
  await expect(f.recovery.continueWithOldKey('synthetic-parent-token',f.owner)).rejects.toThrow('ROTATION_RECOVERY_UNAVAILABLE')
})
it('invalidates an outstanding parent authorization and ignores cancel after success',async()=>{
  const f=setup();await f.recovery.arm();let finish!:(authorized:boolean)=>void
  f.dep.authorize.mockImplementation(()=>new Promise(resolve=>{finish=resolve}))
  const first=f.recovery.continueWithOldKey('synthetic-parent-token',f.owner)
  await vi.waitFor(()=>expect(f.dep.authorize).toHaveBeenCalledOnce());await f.recovery.cancel();finish(true)
  await expect(first).rejects.toThrow('ROTATION_RECOVERY_CHANGED');expect(f.dep.stopAuth).not.toHaveBeenCalled()
  f.dep.authorize.mockResolvedValue(true);await f.recovery.continueWithOldKey('synthetic-parent-token',f.owner)
  await f.recovery.cancel();expect(f.dep.cancelStarting).not.toHaveBeenCalled();expect(f.dep.startOld).toHaveBeenCalledOnce()
})
it('aborts an in-flight old-service start on cancellation and returns to PIN-only mode',async()=>{
  const f=setup();await f.recovery.arm();let finish!:()=>void
  f.dep.startOld.mockImplementation(()=>new Promise<void>(resolve=>{finish=resolve}))
  const first=f.recovery.continueWithOldKey('synthetic-parent-token',f.owner)
  await vi.waitFor(()=>expect(f.dep.startOld).toHaveBeenCalledOnce())
  await f.recovery.cancel();expect(f.dep.cancelStarting).toHaveBeenCalledOnce();finish()
  await expect(first).rejects.toThrow('ROTATION_RECOVERY_CHANGED')
  expect(f.dep.restoreAuth).toHaveBeenCalledOnce()
})
