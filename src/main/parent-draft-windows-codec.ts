import * as fs from 'node:fs'
import { createHash, randomUUID } from 'node:crypto'
import { dirname, join, parse, resolve } from 'node:path'
import { spawn, spawnSync } from 'node:child_process'
import type { ParentDraftRecoveryCodec } from './parent-draft-recovery'
import { ParentDraftRecoveryError } from './parent-draft-recovery-validation'
import { createDraftCryptoBridge, createDraftCryptoBridgeAsync, type DraftCryptoBridge } from './parent-draft-codec-bridge'
import { windowsDraftHelperEnvironment, type WindowsDraftCommitPort } from './parent-draft-windows-atomic'

const LIMIT = 4 * 1024 * 1024 + 65536 // Original total ciphertext bound; Store retains its 4 MiB snapshot bound.
const MAGIC = Buffer.from('HSC-PDR-DPAPI\0\x01', 'ascii')
const digest = (bytes: Buffer | string): string => createHash('sha256').update(bytes).digest('hex')
function failure(code: string): never { throw new ParentDraftRecoveryError(code) }
const same = (a: string, b: string): boolean => resolve(a).toLowerCase() === resolve(b).toLowerCase()

// Static helper: argv contains only fixed operation/profile/store metadata, never user text, keys or arbitrary commands. No logging.
// DPAPI uses the current Windows account and stable profile-bound entropy, not an app-generated key.
export const windowsDraftDpapiSource = String.raw`using System;
using System.IO;
using System.Text;
using System.Threading;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
public static class HscDraftDpapi {
  const int Limit=4259840;
  static readonly object Gate=new object();
  static byte[] queued=null; static int queuedOp=0; static uint queuedSeq=0; static bool outstanding=false;
  static void Clear(byte[] bytes) { if(bytes!=null) Array.Clear(bytes,0,bytes.Length); }
  static int Crypto(int op,byte[] input,string profile,out byte[] output) {
    byte[] entropy=null; output=null;
    try {
      var utf8=new UTF8Encoding(false,true);
      entropy=Encoding.UTF8.GetBytes("HSC_PARENT_DRAFT_DPAPI_V1\0"+profile);
      if(op==1) { utf8.GetCharCount(input); output=ProtectedData.Protect(input,entropy,DataProtectionScope.CurrentUser); }
      else { output=ProtectedData.Unprotect(input,entropy,DataProtectionScope.CurrentUser); utf8.GetCharCount(output); }
      if(output.Length==0 || output.Length>Limit) { Clear(output);output=null;return 3; }
      return 0;
    } catch(CryptographicException) { Clear(output);output=null;return 4; }
      catch(DecoderFallbackException) { Clear(output);output=null;return 6; }
      catch { Clear(output);output=null;return 5; }
      finally { Clear(entropy); }
  }
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
  [return: MarshalAs(UnmanagedType.Bool)]
  static extern bool MoveFileExW(string source,string target,uint flags);
  static bool Hex(string value,int start,int count) { for(int i=start;i<start+count;i++) {char c=value[i];if(!((c>='0'&&c<='9')||(c>='a'&&c<='f')))return false;}return true; }
  static int Commit(byte[] input,string root,out byte[] output) {
    output=null;
    try {
      if(input.Length<8 || input.Length>256)return 2;
      uint first=U32(input,0);if(first==0 || first>128 || first+8>input.Length)return 2;
      uint second=U32(input,4+(int)first);if(second==0 || second>128 || first+second+8!=input.Length)return 2;
      var utf8=new UTF8Encoding(false,true);
      string from=utf8.GetString(input,4,(int)first),to=utf8.GetString(input,8+(int)first,(int)second);
      if(from.Length!=57 || !from.StartsWith(".pending-",StringComparison.Ordinal) || !Hex(from,9,48) || to.Length!=68 || !to.EndsWith(".pdr",StringComparison.Ordinal) || !Hex(to,0,64))return 2;
      string source=Path.Combine(root,from),target=Path.Combine(root,to);
      if(!Directory.Exists(root) || (File.GetAttributes(root)&FileAttributes.ReparsePoint)!=0 || (File.GetAttributes(source)&FileAttributes.ReparsePoint)!=0 || (File.Exists(target)&&(File.GetAttributes(target)&FileAttributes.ReparsePoint)!=0))return 2;
      if(!String.Equals(Path.GetDirectoryName(Path.GetFullPath(source)),root,StringComparison.OrdinalIgnoreCase) || !String.Equals(Path.GetDirectoryName(Path.GetFullPath(target)),root,StringComparison.OrdinalIgnoreCase))return 2;
      if(!MoveFileExW(source,target,0x1|0x8))return 3;
      using(var file=new FileStream(target,FileMode.Open,FileAccess.ReadWrite,FileShare.Read,4096,FileOptions.WriteThrough)) { file.Flush(true); }
      output=Encoding.ASCII.GetBytes("HSC_DRAFT_REPLACED");return 0;
    } catch { Clear(output);output=null;return 5; }
  }
  static uint U32(byte[] bytes,int p) { return ((uint)bytes[p]<<24)|((uint)bytes[p+1]<<16)|((uint)bytes[p+2]<<8)|bytes[p+3]; }
  static void Put32(byte[] bytes,int p,uint n) { bytes[p]=(byte)(n>>24);bytes[p+1]=(byte)(n>>16);bytes[p+2]=(byte)(n>>8);bytes[p+3]=(byte)n; }
  static bool Read(Stream stream,byte[] bytes) {
    int at=0; while(at<bytes.Length) { int n=stream.Read(bytes,at,bytes.Length-at);if(n==0) { if(at!=0) throw new IOException();return false; }at+=n; }return true;
  }
  static void Write(Stream stream,int op,int status,uint seq,byte[] output) {
    byte[] header=new byte[16];
    try { Put32(header,0,0x48535044);header[4]=1;header[5]=(byte)op;header[6]=(byte)status;Put32(header,8,seq);Put32(header,12,(uint)(output==null?0:output.Length));stream.Write(header,0,16);if(output!=null)stream.Write(output,0,output.Length);stream.Flush(); }
    finally { Clear(header); }
  }
  static void Reader(Stream input) {
    byte[] header=new byte[16],payload=null; uint previous=0;
    try {
      for(;;) {
        // This independent reader detects pipe EOF even while the serial DPAPI operation is busy.
        if(!Read(input,header)) Environment.Exit(0);
        uint seq=U32(header,8),length=U32(header,12);int op=header[5];
        if(U32(header,0)!=0x48535044 || header[4]!=1 || header[6]!=0 || header[7]!=0 || (op!=1 && op!=2 && op!=3) || (op==3&&length>256) || seq==0 || seq!=previous+1 || length==0 || length>Limit) Environment.Exit(2);
        payload=new byte[(int)length];if(!Read(input,payload)) Environment.Exit(2);Clear(header);
        lock(Gate) { if(outstanding || queued!=null) Environment.Exit(2);outstanding=true;queued=payload;queuedOp=op;queuedSeq=seq;payload=null;previous=seq;Monitor.Pulse(Gate); }
      }
    } catch { Environment.Exit(2); }
      finally { Clear(header);Clear(payload); }
  }
  static int Serve(string profile,string root) {
    using(var input=Console.OpenStandardInput()) using(var output=Console.OpenStandardOutput()) {
      var reader=new Thread(()=>Reader(input));reader.IsBackground=true;reader.Start();
      Write(output,0,0,0,null);
      for(;;) {
        byte[] request=null,result=null;int op=0;uint seq=0;
        try {
          lock(Gate) { while(queued==null)Monitor.Wait(Gate);request=queued;queued=null;op=queuedOp;seq=queuedSeq; }
          int status=op==3?Commit(request,root,out result):Crypto(op,request,profile,out result);
          lock(Gate) { outstanding=false; }
          Write(output,op,status,seq,result);
        } finally { Clear(request);Clear(result); }
      }
    }
  }
  public static int Main(string[] args) {
    byte[] input=null,output=null,scratch=null;
    try {
      if((args.Length!=2 && args.Length!=3) || (args[0]!="encrypt" && args[0]!="decrypt" && args[0]!="serve") || args[1].Length!=64 || (args[0]=="serve"?args.Length!=3:args.Length!=2)) return 2;
      foreach(char c in args[1]) if(!((c>='0' && c<='9') || (c>='a' && c<='f')))return 2;
      if(args[0]=="serve") { string root=Path.GetFullPath(args[2]);if(!Path.IsPathRooted(args[2]) || !String.Equals(root,args[2],StringComparison.OrdinalIgnoreCase) || !String.Equals(Path.GetFileName(root),"parent-draft-recovery",StringComparison.Ordinal))return 2;return Serve(args[1],root); }
      scratch=new byte[Limit+1];int size=0;
      using(var stream=Console.OpenStandardInput()) { int count;while((count=stream.Read(scratch,size,scratch.Length-size))>0) { size+=count;if(size>Limit)return 3; } }
      if(size==0)return 3;input=new byte[size];Buffer.BlockCopy(scratch,0,input,0,size);Clear(scratch);
      int status=Crypto(args[0]=="encrypt"?1:2,input,args[1],out output);if(status!=0)return status;
      using(var stream=Console.OpenStandardOutput()) { stream.Write(output,0,output.Length);stream.Flush(); }return 0;
    } catch { return 5; }
      finally { Clear(scratch);Clear(input);Clear(output); }
  }
}
`

function directory(target: string): void {
  let current = parse(target).root
  for (const component of target.slice(current.length).split(/[\\/]/).filter(Boolean)) {
    current = join(current, component)
    const stat = fs.lstatSync(current)
    if (!stat.isDirectory() || stat.isSymbolicLink()) failure('DRAFT_RECOVERY_UNSAFE_PATH')
  }
  if (!same(fs.realpathSync(target), target)) failure('DRAFT_RECOVERY_UNSAFE_PATH')
}
function regular(target: string, osFile = false): void {
  const stat = fs.lstatSync(target)
  if (!stat.isFile() || stat.isSymbolicLink() || (!osFile && stat.nlink !== 1) || !same(fs.realpathSync(target), target)) failure('DRAFT_RECOVERY_UNSAFE_FILE')
  directory(dirname(target))
}
function codeBytes(target: string): Buffer {
  regular(target)
  const stat = fs.statSync(target)
  if (stat.size <= 0 || stat.size > 4 * 1024 * 1024) failure('DRAFT_RECOVERY_UNSAFE_FILE')
  const bytes = fs.readFileSync(target)
  regular(target)
  if (bytes.length !== stat.size) failure('DRAFT_RECOVERY_UNSAFE_FILE')
  return bytes
}

export type WindowsDraftCodecOptions = {
  helperDirectory: string
  profileId: string
  legacyCodec: ParentDraftRecoveryCodec
  userDataPath?: string
}

/** Windows draft-only codec. Legacy decryption failure never selects another codec or changes a file. */
export type WindowsDraftRecoveryCodec = ParentDraftRecoveryCodec & { dispose():void; disposeAsync():Promise<void>; readonly commitPort:WindowsDraftCommitPort }
export function createWindowsDraftCodec(options: WindowsDraftCodecOptions): WindowsDraftRecoveryCodec {
  if (process.platform !== 'win32') failure('DRAFT_RECOVERY_ENCRYPTION_UNAVAILABLE')
  if (!/^[a-f0-9]{64}$/.test(options.profileId)) failure('DRAFT_RECOVERY_INVALID_PROFILE')
  const root = resolve(options.helperDirectory), userData = options.userDataPath ?? dirname(root), storeRoot=join(userData,'parent-draft-recovery')
  if (root !== options.helperDirectory || resolve(userData) !== userData || !same(dirname(root), userData)) failure('DRAFT_RECOVERY_UNSAFE_PATH')
  let executable: string, executableHash: string
  try {
    directory(userData)
    if (!fs.existsSync(root)) fs.mkdirSync(root, { mode: 0o700 })
    directory(root)
    const windowsRoot = windowsDraftHelperEnvironment().SystemRoot
    if (!windowsRoot || resolve(windowsRoot) !== windowsRoot) failure('DRAFT_RECOVERY_ENCRYPTION_UNAVAILABLE')
    directory(windowsRoot)
    const framework = join(windowsRoot, 'Microsoft.NET', 'Framework64', 'v4.0.30319')
    const compiler = join(framework, 'csc.exe'), assembly = join(framework, 'System.Security.dll')
    // Only these exact protected framework prerequisites may have intrinsic OS hardlinks.
    regular(compiler, true); regular(assembly, true)
    const id = randomUUID(), source = join(root, `dpapi-${id}.cs`)
    executable = join(root, `dpapi-${id}.exe`)
    try {
      fs.writeFileSync(source, windowsDraftDpapiSource, { flag: 'wx', mode: 0o600 })
      regular(source)
      if (digest(codeBytes(source)) !== digest(windowsDraftDpapiSource) || fs.existsSync(executable)) failure('DRAFT_RECOVERY_UNSAFE_FILE')
      const compiled = spawnSync(compiler, ['/nologo', '/noconfig', '/target:exe', '/optimize+', `/reference:${assembly}`, `/out:${executable}`, source], {
        timeout: 10_000, maxBuffer: 8192, windowsHide: true, shell: false, encoding: 'buffer', env: windowsDraftHelperEnvironment()
      })
      if (compiled.error || compiled.signal || compiled.status !== 0 || compiled.stderr?.length) failure('DRAFT_RECOVERY_ENCRYPTION_UNAVAILABLE')
      executableHash = digest(codeBytes(executable))
    } catch (error) {
      try { fs.unlinkSync(executable) } catch { /* Never admit a failed compilation. */ }
      throw error
    } finally { try { fs.unlinkSync(source) } catch { /* Public static source only. */ } }
  } catch (error) {
    if (error instanceof ParentDraftRecoveryError) throw error
    failure('DRAFT_RECOVERY_ENCRYPTION_UNAVAILABLE')
  }
  return admittedWindowsDraftCodec(options,executable,executableHash)
}

function admittedWindowsDraftCodec(options:WindowsDraftCodecOptions,executable:string,executableHash:string,preparedBridge?:DraftCryptoBridge):WindowsDraftRecoveryCodec {
  const userData=options.userDataPath??dirname(options.helperDirectory),storeRoot=join(userData,'parent-draft-recovery')
  const assertAvailable = () => {
    if(disposed||poisoned)failure('DRAFT_RECOVERY_IO')
    try {
      if (digest(codeBytes(executable)) !== executableHash) failure('DRAFT_RECOVERY_UNSAFE_FILE')
    } catch (error) {
      if (error instanceof ParentDraftRecoveryError) throw error
      failure('DRAFT_RECOVERY_ENCRYPTION_UNAVAILABLE')
    }
  }
  let bridge:DraftCryptoBridge|undefined=preparedBridge, disposed=false, poisoned=false
  const dispose=()=>{
    if(disposed)return
    try { bridge?.dispose() } catch { failure('DRAFT_RECOVERY_IO') }
    disposed=true
  }
  // Explicit will-quit disposal is primary. Abrupt Main death closes the pipe; the helper EOF reader retires it.
  const poisonCommit=()=>{poisoned=true;try{bridge?.dispose()}catch{}}
  const invoke = (operation: 'encrypt' | 'decrypt' | 'replaceAndFlush', input: Buffer): Buffer => {
    if(disposed||poisoned)failure('DRAFT_RECOVERY_IO')
    assertAvailable()
    if (input.length === 0 || input.length > LIMIT) failure('DRAFT_RECOVERY_TOO_LARGE')
    let result:{status:number;output:Buffer}
    try {
      bridge??=createDraftCryptoBridge({executable,args:['serve',options.profileId,storeRoot],env:windowsDraftHelperEnvironment()})
      result=bridge.invoke(operation,input)
    } catch { if(operation==='replaceAndFlush')poisonCommit();return failure('DRAFT_RECOVERY_IO') }
    if(operation==='replaceAndFlush' && result.status!==0){result.output.fill(0);poisonCommit();failure('DRAFT_RECOVERY_IO')}
    if(result.status===4 && result.output.length===0)failure(operation==='decrypt'?'DRAFT_RECOVERY_CORRUPT_OR_KEY_UNAVAILABLE':'DRAFT_RECOVERY_ENCRYPTION_UNAVAILABLE')
    if(result.status!==0 || result.output.length===0 || result.output.length>LIMIT) {result.output.fill(0);failure('DRAFT_RECOVERY_IO')}
    return result.output
  }
  const commitPort:WindowsDraftCommitPort=Object.freeze({root:storeRoot,assertAvailable,
    replaceAndFlush(from:string,to:string):void {
      if(!/^\.pending-[a-f0-9]{48}$/.test(from)||!/^[a-f0-9]{64}\.pdr$/.test(to))failure('DRAFT_RECOVERY_IO')
      const a=Buffer.from(from,'utf8'),b=Buffer.from(to,'utf8'),request=Buffer.alloc(8+a.length+b.length);let reply:Buffer|undefined
      try { request.writeUInt32BE(a.length,0);request.set(a,4);request.writeUInt32BE(b.length,4+a.length);request.set(b,8+a.length)
        reply=invoke('replaceAndFlush',request)
        if(!reply.equals(Buffer.from('HSC_DRAFT_REPLACED','ascii'))){poisonCommit();failure('DRAFT_RECOVERY_IO')}
      } finally {a.fill(0);b.fill(0);request.fill(0);reply?.fill(0)}
    }
  })
  return {
    assertAvailable,dispose,disposeAsync:async()=>{await bridge?.disposeAsync();disposed=true},commitPort,
    encrypt(plain) {
      if (typeof plain !== 'string') failure('DRAFT_RECOVERY_INVALID_SNAPSHOT')
      const bytes = Buffer.from(plain, 'utf8')
      let encrypted:Buffer|undefined
      try {
        if (new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes) !== plain) failure('DRAFT_RECOVERY_INVALID_SNAPSHOT')
        encrypted=invoke('encrypt',bytes)
        if(encrypted.length+MAGIC.length>LIMIT)failure('DRAFT_RECOVERY_TOO_LARGE')
        return Buffer.concat([MAGIC,encrypted])
      } finally {bytes.fill(0);encrypted?.fill(0)}
    },
    decrypt(cipher) {
      if(disposed||poisoned)failure('DRAFT_RECOVERY_IO')
      if (!Buffer.isBuffer(cipher) || cipher.length === 0 || cipher.length > LIMIT) failure('DRAFT_RECOVERY_CORRUPT_OR_KEY_UNAVAILABLE')
      if (cipher.subarray(0, MAGIC.length).equals(MAGIC)) {
        const plain = invoke('decrypt', cipher.subarray(MAGIC.length))
        try { return new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(plain) }
        catch { return failure('DRAFT_RECOVERY_IO') }
        finally { plain.fill(0) }
      }
      const version = cipher.subarray(0, 3)
      if ((version.equals(Buffer.from('v10')) || version.equals(Buffer.from('v11'))) && cipher.length > 3) return options.legacyCodec.decrypt(cipher)
      return failure('DRAFT_RECOVERY_CORRUPT_OR_KEY_UNAVAILABLE')
    }
  }
}


/** Public static compiler/READY preparation only: no plaintext, Parent lease or renderer authority. */
async function prepareWindowsDraftCodec(options:WindowsDraftCodecOptions,signal:AbortSignal):Promise<WindowsDraftRecoveryCodec> {
  if(process.platform!=='win32')failure('DRAFT_RECOVERY_ENCRYPTION_UNAVAILABLE')
  if(!/^[a-f0-9]{64}$/.test(options.profileId))failure('DRAFT_RECOVERY_INVALID_PROFILE')
  const root=resolve(options.helperDirectory),userData=options.userDataPath??dirname(root),storeRoot=join(userData,'parent-draft-recovery')
  if(root!==options.helperDirectory||resolve(userData)!==userData||!same(dirname(root),userData))failure('DRAFT_RECOVERY_UNSAFE_PATH')
  directory(userData);if(!fs.existsSync(root))fs.mkdirSync(root,{mode:0o700});directory(root)
  const windowsRoot=windowsDraftHelperEnvironment().SystemRoot
  if(!windowsRoot||resolve(windowsRoot)!==windowsRoot)failure('DRAFT_RECOVERY_ENCRYPTION_UNAVAILABLE')
  directory(windowsRoot)
  const framework=join(windowsRoot,'Microsoft.NET','Framework64','v4.0.30319'),compiler=join(framework,'csc.exe'),assembly=join(framework,'System.Security.dll')
  regular(compiler,true);regular(assembly,true)
  const id=randomUUID(),source=join(root,`dpapi-${id}.cs`),executable=join(root,`dpapi-${id}.exe`)
  let executableHash:string
  try {
    if(signal.aborted)throw Error('DRAFT_RECOVERY_PREPARATION_CANCELLED')
    fs.writeFileSync(source,windowsDraftDpapiSource,{flag:'wx',mode:0o600});regular(source)
    if(digest(codeBytes(source))!==digest(windowsDraftDpapiSource)||fs.existsSync(executable))failure('DRAFT_RECOVERY_UNSAFE_FILE')
    await new Promise<void>((resolve,reject)=>{
      let error=false,exited:{code:number|null;signal:NodeJS.Signals|null}|undefined,closed=false,unknown=false,stdout=0,stderr=0
      const child=spawn(compiler,['/nologo','/noconfig','/target:exe','/optimize+',`/reference:${assembly}`,`/out:${executable}`,source],{windowsHide:true,shell:false,env:windowsDraftHelperEnvironment(),stdio:['ignore','pipe','pipe']})
      let cleanupTimer:ReturnType<typeof setTimeout>|undefined
      const closeBound=()=>{if(!cleanupTimer)cleanupTimer=setTimeout(()=>{error=true;unknown=true;clearTimeout(timer);signal.removeEventListener('abort',stop);reject(Error('DRAFT_RECOVERY_ORIGINAL_CLOSE_UNCONFIRMED'))},5000)}
      const stop=()=>{if(!closed&&!unknown){error=true;closeBound();if(exited===undefined)try{child.kill()}catch{}}}
      const timer=setTimeout(stop,10000)
      signal.addEventListener('abort',stop,{once:true})
      child.once('error',()=>{error=true;closeBound()})
      child.once('exit',(code,signal)=>{exited={code,signal};closeBound()})
      child.stdout.on('data',(b:Buffer)=>{stdout+=b.length;b.fill(0);if(stdout>8192)stop()})
      child.stderr.on('data',(b:Buffer)=>{stderr+=b.length;b.fill(0);if(stderr>8192)stop()})
      child.once('close',(code,closedSignal)=>{closed=true;clearTimeout(timer);clearTimeout(cleanupTimer);signal.removeEventListener('abort',stop)
        if(signal.aborted){reject(Error('DRAFT_RECOVERY_PREPARATION_CANCELLED'));return}
        if(error||!exited||exited.code!==code||exited.signal!==closedSignal||code!==0||closedSignal!==null||stderr||stdout>8192){reject(Error('DRAFT_RECOVERY_ENCRYPTION_UNAVAILABLE'));return}
        resolve()
      })
      if(signal.aborted)stop()
    })
    regular(compiler,true);regular(assembly,true)
    if(digest(codeBytes(source))!==digest(windowsDraftDpapiSource))failure('DRAFT_RECOVERY_UNSAFE_FILE')
    executableHash=digest(codeBytes(executable))
  }catch(error){try{fs.unlinkSync(executable)}catch{};throw error}
  finally{try{fs.unlinkSync(source)}catch{}}
  if(signal.aborted)throw Error('DRAFT_RECOVERY_PREPARATION_CANCELLED')
  const bridge=await createDraftCryptoBridgeAsync({executable,args:['serve',options.profileId,storeRoot],env:windowsDraftHelperEnvironment()},signal)
  if(signal.aborted){try{await bridge.disposeAsync()}catch{throw Error('DRAFT_RECOVERY_ORIGINAL_CLOSE_UNCONFIRMED')};throw Error('DRAFT_RECOVERY_PREPARATION_CANCELLED')}
  return admittedWindowsDraftCodec(options,executable,executableHash,bridge)
}

/** One profile-scoped cold attempt. A closed-quit cancellation can start a new turn; unknown closure cannot. */
export function createWindowsDraftCodecPreparation(options:WindowsDraftCodecOptions) {
  let ready:WindowsDraftRecoveryCodec|undefined,pending:Promise<WindowsDraftRecoveryCodec>|undefined,controller:AbortController|undefined,accepting=true,cancelledClosed=false
  return {
    prepare():Promise<WindowsDraftRecoveryCodec>{
      if(!accepting)return Promise.reject(Error('DRAFT_RECOVERY_PREPARATION_CANCELLED'))
      if(ready)return Promise.resolve(ready)
      if(!pending){controller=new AbortController();pending=prepareWindowsDraftCodec(options,controller.signal).then(value=>{ready=value;return value},error=>{cancelledClosed=error instanceof Error&&error.message==='DRAFT_RECOVERY_PREPARATION_CANCELLED';throw error});void pending.catch(()=>{})}
      return pending
    },
    current():WindowsDraftRecoveryCodec{if(!ready)failure('DRAFT_RECOVERY_IO');return ready},
    async quiesce():Promise<void>{accepting=false;if(pending&&!ready)controller?.abort();if(pending)try{await pending}catch(error){if(error instanceof Error&&error.message==='DRAFT_RECOVERY_ORIGINAL_CLOSE_UNCONFIRMED')throw error}},
    resume():void{accepting=true;if(cancelledClosed&&!ready){pending=undefined;controller=undefined;cancelledClosed=false}},
    async dispose():Promise<void>{await this.quiesce();if(ready)await ready.disposeAsync()}
  }
}
