// Pure native-boundary controls: real temporary files, mocked compiler/helper. Not a Windows/DPAPI proof.
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'
import { spawnSync } from 'node:child_process'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { createWindowsDraftCodec, windowsDraftDpapiSource } from './parent-draft-windows-codec'
import { createDraftCryptoBridge } from './parent-draft-codec-bridge'
import { createWindowsDraftAtomicIO } from './parent-draft-windows-atomic'

vi.mock('node:child_process', () => {
  const spawnSync = vi.fn()
  return { spawnSync, default: { spawnSync } }
})
vi.mock('./parent-draft-codec-bridge',()=>({createDraftCryptoBridge:vi.fn()}))
const mockBridge=vi.mocked(createDraftCryptoBridge)
const codecs:ReturnType<typeof createWindowsDraftCodec>[]=[]
let helperResult:(args:any[],options:any)=>any
const platform = Object.getOwnPropertyDescriptor(process, 'platform')!
const mockSpawn = vi.mocked(spawnSync)
const profile = 'a'.repeat(64), secret = 'private-test-body-🧠'
let root: string, userData: string, helper: string, framework: string, executable: string
let mode: 'normal' | 'timeout' | 'stderr' | 'nonzero' | 'invalid-utf8' | 'overflow' | 'empty' | 'throw' | 'io-status' | 'signal' | 'crypto-with-stderr' | 'prefix-overflow' | 'bad-compile' = 'normal'
let legacy: { assertAvailable: Mock<() => void>; encrypt: Mock<(plain: string) => Buffer>; decrypt: Mock<(cipher: Buffer) => string> }
const ok = (stdout = Buffer.alloc(0)) => ({ status: 0, signal: null, error: undefined, stdout, stderr: Buffer.alloc(0), pid: 1, output: [] }) as any

beforeEach(() => {
  root = fs.mkdtempSync(path.join(fs.realpathSync(os.tmpdir()), 'draft-codec-test-'))
  userData = path.join(root, 'synthetic-profile'); fs.mkdirSync(userData)
  helper = path.join(userData, 'draft-crypto-helper')
  const system = path.join(root, 'Windows'); framework = path.join(system, 'Microsoft.NET', 'Framework64', 'v4.0.30319')
  fs.mkdirSync(framework, { recursive: true })
  for (const name of ['csc.exe', 'System.Security.dll']) fs.writeFileSync(path.join(framework, name), 'synthetic protected OS prerequisite')
  Object.defineProperty(process, 'platform', { ...platform, value: 'win32' })
  vi.stubEnv('SystemRoot', system); vi.stubEnv('USERPROFILE', userData)
  vi.stubEnv('HSC_PARENT_SESSION_TOKEN', 'never-inherit'); vi.stubEnv('OPENAI_API_KEY', 'never-inherit')
  vi.stubEnv('HTTPS_PROXY', 'never-inherit'); vi.stubEnv('NODE_OPTIONS', 'never-inherit'); vi.stubEnv('COMPLUS_Profiler', 'never-inherit')
  legacy = { assertAvailable: vi.fn(() => {}), encrypt: vi.fn((plain: string) => Buffer.from(plain)), decrypt: vi.fn(() => 'legacy plaintext') }
  executable = ''; mode = 'normal'; mockSpawn.mockReset();mockBridge.mockReset()
  mockSpawn.mockImplementation((bin: any, args: any = [], options: any = {}) => {
    if (path.basename(bin) === 'csc.exe') {
      if (mode === 'bad-compile') return { ...ok(), status: 1 }
      const out = args.find((arg: string) => arg.startsWith('/out:')).slice(5)
      fs.writeFileSync(out, 'synthetic fixed helper bytes'); executable = out
      return ok()
    }
    return helperResult(args,options)
  })
  helperResult=(args,options)=>{
    if (mode === 'timeout') return { ...ok(), error: Object.assign(Error('private error must not escape'), { code: 'ETIMEDOUT' }) }
    if (mode === 'stderr') return { ...ok(), stderr: Buffer.from('private error must not escape') }
    if (mode === 'nonzero') return { ...ok(), status: 4 }
    if (mode === 'empty') return ok()
    if (mode === 'io-status') return { ...ok(), status: 5 }
    if (mode === 'signal') return { ...ok(), status: null, signal: 'SIGTERM' }
    if (mode === 'crypto-with-stderr') return { ...ok(), status: 4, stderr: Buffer.from('private unknown fault') }
    if (mode === 'prefix-overflow') return ok(Buffer.alloc(4 * 1024 * 1024 + 65536))
    if (mode === 'overflow') return ok(Buffer.alloc(4 * 1024 * 1024 + 65537))
    if (mode === 'invalid-utf8') return ok(Buffer.from([0xff]))
    if (mode === 'throw') throw Error('private spawn exception')
    if(args[0]==='replaceAndFlush')return ok(Buffer.from('HSC_DRAFT_REPLACED'))
    if (args[0] !== 'encrypt' && args[0] !== 'decrypt') return { ...ok(Buffer.from('HSC_DRAFT_REPLACED')), stdout: 'HSC_DRAFT_REPLACED', stderr: '' }
    const prefix = Buffer.from('synthetic-dpapi:' + args[1] + ':')
    if (args[0] === 'encrypt') return ok(Buffer.concat([prefix, options.input]))
    if (!options.input.subarray(0, prefix.length).equals(prefix)) return { ...ok(), status: 4 }
    return ok(options.input.subarray(prefix.length))
  }
  mockBridge.mockImplementation(options=>({
    invoke(operation,input){const result=helperResult([operation,options.args[1]],{input});if(result.error||result.signal||result.stderr?.length||!Buffer.isBuffer(result.stdout))throw Error('DRAFT_RECOVERY_IO');return {status:result.status,output:Buffer.from(result.stdout)}},
    dispose:vi.fn(),disposeAsync:vi.fn(async()=>{}),inspect:()=>({closed:false,terminated:false,retired:false,requestZero:true,responseZero:true,poisoned:false})
  }))
})
afterEach(() => {
  for(const codec of codecs.splice(0))codec.dispose()
  Object.defineProperty(process, 'platform', platform); vi.unstubAllEnvs()
  fs.rmSync(root, { recursive: true, force: true })
})
const create = (id = profile) => {const codec=createWindowsDraftCodec({ helperDirectory: helper, userDataPath: userData, profileId: id, legacyCodec: legacy });codecs.push(codec);return codec}

describe('versioned Windows draft boundary', () => {
  it('new writes and reads use only DPAPI; exact bytes including emoji survive; no safeStorage initialization', () => {
    const codec = create(); const encrypted = codec.encrypt(secret)
    expect(encrypted.subarray(0, 15).toString('ascii')).toBe('HSC-PDR-DPAPI\0\x01')
    expect(codec.decrypt(encrypted)).toBe(secret)
    expect(legacy.assertAvailable).not.toHaveBeenCalled(); expect(legacy.encrypt).not.toHaveBeenCalled(); expect(legacy.decrypt).not.toHaveBeenCalled()
  })
  it.each(['v10', 'v11'])('only %s legacy reads delegate to the original codec', version => {
    const codec = create(), cipher = Buffer.from(version + 'original opaque bytes')
    expect(codec.decrypt(cipher)).toBe('legacy plaintext')
    expect(legacy.decrypt).toHaveBeenCalledExactlyOnceWith(cipher)
    expect(legacy.encrypt).not.toHaveBeenCalled(); expect(mockSpawn).toHaveBeenCalledTimes(1)
  })
  it('preserves the exact original legacy error and never tries DPAPI on failure', () => {
    const codec = create(), original = Error('synthetic legacy failure')
    legacy.decrypt.mockImplementation(() => { throw original })
    let caught: unknown; try { codec.decrypt(Buffer.from('v10opaque')) } catch (error) { caught = error }
    expect(caught).toBe(original); expect(mockSpawn).toHaveBeenCalledTimes(1)
  })
  it.each(['v12unknown', 'plaintext', 'HSC-PDR-DPAPI\0\x02bad', 'v10', ''])('rejects unknown or empty input %j without fallback', value => {
    const codec = create(); expect(() => codec.decrypt(Buffer.from(value))).toThrow('DRAFT_RECOVERY_CORRUPT_OR_KEY_UNAVAILABLE')
    expect(legacy.decrypt).not.toHaveBeenCalled(); expect(mockSpawn).toHaveBeenCalledTimes(1)
  })
  it('does not mask high bits into a recognized legacy prefix or normalize UTF8 BOM data', () => {
    const codec = create()
    expect(() => codec.decrypt(Buffer.from([0xf6, 0xb1, 0xb0, 1]))).toThrow('DRAFT_RECOVERY_CORRUPT_OR_KEY_UNAVAILABLE')
    expect(legacy.decrypt).not.toHaveBeenCalled()
    expect(codec.decrypt(codec.encrypt('\ufeff'+secret))).toBe('\ufeff'+secret)
  })
  it('wrong profile entropy and corrupted new cipher reject without invoking legacy', () => {
    const codec = create(), cipher = codec.encrypt(secret), other = create('b'.repeat(64))
    expect(() => other.decrypt(cipher)).toThrow('DRAFT_RECOVERY_CORRUPT_OR_KEY_UNAVAILABLE')
    cipher[cipher.length - 1] ^= 1 // The strict UTF8 decoder rejects this corrupted emoji in the synthetic helper.
    mode = 'nonzero'; expect(() => codec.decrypt(cipher)).toThrow('DRAFT_RECOVERY_CORRUPT_OR_KEY_UNAVAILABLE')
    expect(legacy.decrypt).not.toHaveBeenCalled()
  })
  it('accepts a 4 MiB canonical body plus small envelope, retaining the original total ciphertext cap', () => {
    const codec = create(), text = JSON.stringify({ snapshot: 'x'.repeat(4 * 1024 * 1024), profileId: profile })
    expect(codec.decrypt(codec.encrypt(text))).toBe(text)
  })
  it('rejects input/output overflow and unpaired surrogates, not truncating data', () => {
    const codec = create()
    expect(() => codec.encrypt('x'.repeat(4 * 1024 * 1024 + 65537))).toThrow('DRAFT_RECOVERY_TOO_LARGE')
    expect(() => codec.encrypt('\ud800')).toThrow('DRAFT_RECOVERY_INVALID_SNAPSHOT')
    expect(() => codec.decrypt(Buffer.alloc(4 * 1024 * 1024 + 65537))).toThrow('DRAFT_RECOVERY_CORRUPT_OR_KEY_UNAVAILABLE')
    mode = 'overflow'; expect(() => codec.encrypt(secret)).toThrow('DRAFT_RECOVERY_IO')
    mode = 'prefix-overflow'; expect(() => codec.encrypt(secret)).toThrow('DRAFT_RECOVERY_TOO_LARGE')
  })
  it.each(['timeout', 'stderr', 'nonzero', 'empty', 'invalid-utf8', 'io-status', 'signal', 'crypto-with-stderr', 'throw'] as const)('holds new cipher failure %s and never falls back', failure => {
    const codec = create(), cipher = codec.encrypt(secret); mode = failure
    expect(() => codec.decrypt(cipher)).toThrow(failure === 'nonzero' ? 'DRAFT_RECOVERY_CORRUPT_OR_KEY_UNAVAILABLE' : 'DRAFT_RECOVERY_IO'); expect(legacy.decrypt).not.toHaveBeenCalled()
  })
  it('uses binary stdin/stdout, bounded fixed args and OS-only environment for compiler and codec', () => {
    const codec = create(); codec.encrypt(secret)
    for (const [, args, options] of mockSpawn.mock.calls as any[]) {
      expect(args.join(' ')).not.toContain(secret); expect(options.shell).toBe(false)
      expect(Object.keys(options.env).sort()).toEqual(expect.arrayContaining(['SystemRoot', 'USERPROFILE']))
      expect(Object.keys(options.env).some(name => /HSC_|KEY|TOKEN|PROXY|NODE_OPTIONS|COMPLUS/i.test(name))).toBe(false)
      expect(options.timeout).toBeLessThanOrEqual(args[0] === 'encrypt' ? 2000 : 10_000)
    }
    const options=mockBridge.mock.calls[0][0]
    expect(options.args).toEqual(['serve',profile,path.join(userData,'parent-draft-recovery')]);expect(options.executable).toBe(executable)
    expect(Object.keys(options.env).some(name=>/HSC_|KEY|TOKEN|PROXY|NODE_OPTIONS|COMPLUS/i.test(name))).toBe(false)
    expect(mockSpawn).toHaveBeenCalledTimes(1)
    expect(fs.readdirSync(helper)).toEqual([path.basename(executable)]) // No plaintext temp or source file remains.
  })
})

describe('fixed helper admission and atomic spawn environment', () => {
  it('rejects modified helper bytes before any operation', () => {
    const codec = create(); fs.appendFileSync(executable, 'tampered')
    expect(() => codec.encrypt(secret)).toThrow('DRAFT_RECOVERY_UNSAFE_FILE'); expect(mockSpawn).toHaveBeenCalledTimes(1)
  })
  it.each(['symlink', 'hardlink'] as const)('rejects helper %s aliases', type => {
    const codec = create(), original = path.join(root, 'other.exe'); fs.renameSync(executable, original)
    if (type === 'symlink') fs.symlinkSync(original, executable)
    else fs.linkSync(original, executable)
    expect(() => codec.encrypt(secret)).toThrow('DRAFT_RECOVERY_UNSAFE_FILE'); expect(mockSpawn).toHaveBeenCalledTimes(1)
  })
  it('allows intrinsic hardlinks only on the two fixed OS prerequisites', () => {
    for (const name of ['csc.exe', 'System.Security.dll']) fs.linkSync(path.join(framework, name), path.join(root, name))
    expect(() => create()).not.toThrow()
  })
  it('rejects a symlink compiler and missing System.Security instead of downloading or falling back', () => {
    const compiler = path.join(framework, 'csc.exe'), target = path.join(root, 'compiler.exe')
    fs.renameSync(compiler, target); fs.symlinkSync(target, compiler)
    expect(() => create()).toThrow('DRAFT_RECOVERY_UNSAFE_FILE'); expect(mockSpawn).not.toHaveBeenCalled()
    fs.unlinkSync(compiler); fs.renameSync(target, compiler); fs.unlinkSync(path.join(framework, 'System.Security.dll'))
    expect(() => create()).toThrow('DRAFT_RECOVERY_ENCRYPTION_UNAVAILABLE'); expect(mockSpawn).not.toHaveBeenCalled()
  })
  it('rejects non-Windows, invalid profile, escaping helper, and failed compiler', () => {
    Object.defineProperty(process, 'platform', { ...platform, value: 'darwin' })
    expect(() => create()).toThrow('DRAFT_RECOVERY_ENCRYPTION_UNAVAILABLE')
    Object.defineProperty(process, 'platform', { ...platform, value: 'win32' })
    expect(() => create('not-a-profile')).toThrow('DRAFT_RECOVERY_INVALID_PROFILE')
    expect(() => createWindowsDraftCodec({ helperDirectory: root, userDataPath: userData, profileId: profile, legacyCodec: legacy })).toThrow('DRAFT_RECOVERY_UNSAFE_PATH')
    mode = 'bad-compile'; expect(() => create()).toThrow('DRAFT_RECOVERY_ENCRYPTION_UNAVAILABLE')
  })
  it('applies the same OS-only environment to both original atomic compiler and commit calls', () => {
    const store = path.join(userData, 'parent-draft-recovery'); fs.mkdirSync(store)
    const atomic = createWindowsDraftAtomicIO(store, path.join(userData, 'commit-helper'))
    const temp = path.join(store, 'temp'); fs.writeFileSync(temp, 'synthetic cipher')
    atomic.replaceAndFlush(temp, path.join(store, 'final'))
    expect(mockSpawn).toHaveBeenCalledTimes(2)
    for (const [, , options] of mockSpawn.mock.calls as any[]) {
      expect(Object.keys(options.env).some(name => /HSC_|KEY|TOKEN|PROXY|NODE_OPTIONS|COMPLUS/i.test(name))).toBe(false)
      expect(options.env.USERPROFILE).toBe(userData)
    }
  })
  it('static source uses CurrentUser purpose/profile entropy and no file/log/key persistence', () => {
    expect(windowsDraftDpapiSource).toContain('DataProtectionScope.CurrentUser')
    expect(windowsDraftDpapiSource).toContain('HSC_PARENT_DRAFT_DPAPI_V1\\0')
    const crypto=windowsDraftDpapiSource.slice(windowsDraftDpapiSource.indexOf('static int Crypto('),windowsDraftDpapiSource.indexOf('  [DllImport'))
    expect(crypto).not.toMatch(/File\.|Console\.(Write|Error)|Process\.|LocalMachine/)
    expect(windowsDraftDpapiSource).toContain('MoveFileExW(source,target,0x1|0x8)');expect(windowsDraftDpapiSource).toContain('file.Flush(true)')
  })
})

describe('private persistent codec lifetime',()=>{
  it('starts one lazy bridge for multiple operations, while legacy remains decrypt-only',()=>{
    const codec=create();codec.decrypt(Buffer.from('v10opaque'));expect(mockBridge).not.toHaveBeenCalled()
    for(let i=0;i<5;i++)expect(codec.decrypt(codec.encrypt(secret))).toBe(secret)
    expect(mockBridge).toHaveBeenCalledTimes(1);expect(mockSpawn).toHaveBeenCalledTimes(1)
    fs.appendFileSync(executable,'tampered');expect(()=>codec.encrypt(secret)).toThrow('DRAFT_RECOVERY_UNSAFE_FILE')
  })
  it('explicit disposal closes the owned bridge once and refuses all later operations',()=>{
    const codec=create();codec.encrypt(secret);const bridge=mockBridge.mock.results[0].value
    codec.dispose();codec.dispose();expect(bridge.dispose).toHaveBeenCalledTimes(1)
    expect(()=>codec.encrypt(secret)).toThrow('DRAFT_RECOVERY_IO');expect(()=>codec.decrypt(Buffer.from('v10opaque'))).toThrow('DRAFT_RECOVERY_IO')
  })
  it('does not mark disposal complete on missing child-close acknowledgement; retry can observe close',()=>{
    const codec=create();codec.encrypt(secret);const bridge=mockBridge.mock.results[0].value
    vi.mocked(bridge.dispose).mockImplementationOnce(()=>{throw Error('synthetic close missing')})
    expect(()=>codec.dispose()).toThrow('DRAFT_RECOVERY_IO');expect(()=>codec.dispose()).not.toThrow()
    expect(bridge.dispose).toHaveBeenCalledTimes(2)
  })
})

describe('fixed-store private commit port',()=>{
 it('uses the same native bridge and strict bounded two-basename frame; buffers are zero after return',()=>{
  const codec=create(),from='.pending-'+'a'.repeat(48),to='b'.repeat(64)+'.pdr';expect(codec.commitPort.root).toBe(path.join(userData,'parent-draft-recovery'))
  codec.encrypt(secret);const bridge=mockBridge.mock.results[0].value;let held:Buffer|undefined
  const original=bridge.invoke;bridge.invoke=vi.fn((operation,input)=>{if(operation==='replaceAndFlush'){expect(input.length).toBe(133);expect(input.readUInt32BE(0)).toBe(57);expect(input.subarray(4,61).toString()).toBe(from);expect(input.readUInt32BE(61)).toBe(68);expect(input.subarray(65).toString()).toBe(to);held=input}return original(operation,input)})
  codec.commitPort.replaceAndFlush(from,to);expect(held?.every(x=>x===0)).toBe(true);expect(mockBridge).toHaveBeenCalledTimes(1);expect(mockSpawn).toHaveBeenCalledTimes(1)
 })
 it.each([['../bad','b'.repeat(64)+'.pdr'],['.pending-'+'a'.repeat(48),'b'.repeat(64)+'.pdr:ads'],['.pending-'+'A'.repeat(48),'b'.repeat(64)+'.pdr']])('rejects metadata outside the exact basename pair before native call', (from,to)=>{
  const codec=create();expect(()=>codec.commitPort.replaceAndFlush(from,to)).toThrow('DRAFT_RECOVERY_IO');expect(mockBridge).not.toHaveBeenCalled()
 })
 it('native commit crypto/status4 is IO and permanently poisons; it never becomes decrypt-corruption or a fallback',()=>{
  const codec=create();codec.encrypt(secret);mode='nonzero';expect(()=>codec.commitPort.replaceAndFlush('.pending-'+'a'.repeat(48),'b'.repeat(64)+'.pdr')).toThrow('DRAFT_RECOVERY_IO');mode='normal'
  expect(()=>codec.encrypt(secret)).toThrow('DRAFT_RECOVERY_IO');expect(mockSpawn).toHaveBeenCalledTimes(1);expect(legacy.decrypt).not.toHaveBeenCalled()
 })
 it('wrong success ACK poisons the same session and cannot silently return stored',()=>{
  const codec=create();codec.encrypt(secret);mockBridge.mock.results[0].value.invoke=vi.fn(()=>({status:0,output:Buffer.from('WRONG')}))
  expect(()=>codec.commitPort.replaceAndFlush('.pending-'+'a'.repeat(48),'b'.repeat(64)+'.pdr')).toThrow('DRAFT_RECOVERY_IO');expect(()=>codec.encrypt(secret)).toThrow('DRAFT_RECOVERY_IO')
 })
})
