// @vitest-environment node
import { expect, it } from 'vitest'
import { executableIdentity, queryProcessIdentity, sameProcessIdentity, windowsPinnedStopScript, windowsQueryScript } from './game-process-identity'
it('queries only this owned process and distinguishes birth/executable changes',async()=>{
  const own=await queryProcessIdentity(process.pid)
  expect(own?.pid).toBe(process.pid)
  expect(own!.executable).toEqual(await executableIdentity(process.execPath))
  expect(sameProcessIdentity(own!,{...own!,birth:'reused-pid'})).toBe(false)
  expect(sameProcessIdentity(own!,{...own!,executable:{...own!.executable,pathHash:'different'}})).toBe(false)
})
it('Windows query includes exact creation identity and image, never family command line',()=>{
  const script=windowsQueryScript(123)
  expect(script).toContain('ProcessId = 123')
  expect(script).toContain('CreationDate.ToUniversalTime().Ticks')
  expect(script).toContain('ExecutablePath')
  expect(script).not.toContain('CommandLine')
  expect(()=>windowsQueryScript(Number.NaN)).toThrow()
})
it('Windows stop pins a kernel handle, compares birth/image before taskkill and closes the handle last',()=>{
  const script=windowsPinnedStopScript({pid:123,birth:'win32:638900000000000000',executable:{pathHash:'a'.repeat(64),fileId:'test'}})
  expect(script.indexOf('IntPtr h=OpenProcess')).toBeLessThan(script.indexOf('GetProcessTimes(h'))
  expect(script.indexOf('actual!=imageHash')).toBeLessThan(script.indexOf('Process.Start(info)'))
  expect(script).toContain('finally{CloseHandle(h);}')
  expect(script).toContain('killer.ExitCode==0&&WaitForSingleObject(h,1000)==0')
  expect(()=>windowsPinnedStopScript({pid:123,birth:'win32:bad;command',executable:{pathHash:'a'.repeat(64),fileId:'test'}})).toThrow()
})
