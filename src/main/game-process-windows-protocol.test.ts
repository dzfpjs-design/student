// @vitest-environment node
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
const mocks=vi.hoisted(()=>({run:vi.fn(),realpath:vi.fn(),stat:vi.fn()}))
vi.mock('node:child_process',async()=>{
  const original=await vi.importActual<typeof import('node:child_process')>('node:child_process')
  const execFile=Object.assign(vi.fn(),{[Symbol.for('nodejs.util.promisify.custom')]:mocks.run})
  return {...original,execFile}
})
vi.mock('node:fs/promises',async()=>({...await vi.importActual('node:fs/promises'),realpath:mocks.realpath,stat:mocks.stat}))
import { queryProcessIdentity, stopPinnedWindowsProcess } from './game-process-identity'
const identity={pid:123,birth:'win32:638900000000000000',executable:{pathHash:'a'.repeat(64),fileId:'fixture'}}
beforeEach(()=>{
  vi.stubGlobal('process',Object.defineProperty(Object.create(process),'platform',{value:'win32'}))
  mocks.run.mockReset();mocks.realpath.mockReset();mocks.stat.mockReset()
  mocks.realpath.mockImplementation(async(path:string)=>path)
  mocks.stat.mockResolvedValue({isFile:()=>true,dev:1,ino:2,size:3,mtimeMs:4})
})
afterEach(()=>vi.unstubAllGlobals())
it('decodes a Unicode image and exact birth; query is noninteractive and has a bounded timeout',async()=>{
  mocks.run.mockResolvedValue({stdout:JSON.stringify({pid:123,birth:'638900000000000000',image:'C:\\测试\\GAME.exe'}),stderr:''})
  const result=await queryProcessIdentity(123)
  expect(result).toMatchObject({pid:123,birth:identity.birth,executable:{fileId:'1:2:3:4'}})
  expect(mocks.realpath).toHaveBeenCalledWith('C:\\测试\\GAME.exe')
  const [,args,options]=mocks.run.mock.calls[0]
  expect(args.slice(0,3)).toEqual(['-NoProfile','-NonInteractive','-EncodedCommand'])
  expect(Buffer.from(args[3],'base64').toString('utf16le')).toContain('ProcessId = 123')
  expect(options).toMatchObject({timeout:5000,windowsHide:true,encoding:'utf8'})
})
it('distinguishes absent process from access failure without a weaker lookup',async()=>{
  mocks.run.mockResolvedValueOnce({stdout:'null',stderr:''})
  expect(await queryProcessIdentity(123)).toBeNull()
  mocks.run.mockRejectedValueOnce(Error('ACCESS_DENIED'))
  await expect(queryProcessIdentity(123)).rejects.toThrow('ACCESS_DENIED')
  expect(mocks.run).toHaveBeenCalledTimes(2)
  expect(mocks.realpath).not.toHaveBeenCalled()
})
it.each([
  {pid:124,birth:'638900000000000000',image:'C:\\test.exe'},
  {pid:123,birth:638900000000000000,image:'C:\\test.exe'},
  {pid:123,birth:'0',image:'C:\\test.exe'},
  {pid:123,birth:'638900000000000000',image:'   '},
  {pid:123,birth:'not-a-birth',image:'C:\\test.exe'},
])('rejects malformed identity before filesystem lookup: %j',async value=>{
  mocks.run.mockResolvedValue({stdout:JSON.stringify(value),stderr:''})
  await expect(queryProcessIdentity(123)).rejects.toThrow('PROCESS_IDENTITY_UNAVAILABLE')
  expect(mocks.realpath).not.toHaveBeenCalled()
})
it.each(['false','true\nfalse','', 'access denied'])('never turns non-exact stop proof %j into closed',async stdout=>{
  mocks.run.mockResolvedValue({stdout,stderr:''})
  expect(await stopPinnedWindowsProcess(identity)).toBe(false)
  expect(mocks.run).toHaveBeenCalledTimes(1)
})
it('accepts exact pinned-stop proof but timeout remains unknown without retry or PID-only fallback',async()=>{
  mocks.run.mockResolvedValueOnce({stdout:'true\r\n',stderr:''})
  expect(await stopPinnedWindowsProcess(identity)).toBe(true)
  mocks.run.mockRejectedValueOnce(Error('ETIMEDOUT'))
  expect(await stopPinnedWindowsProcess(identity)).toBe(false)
  expect(mocks.run).toHaveBeenCalledTimes(2)
})
