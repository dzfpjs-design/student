import { createHash } from 'node:crypto'
import { execFile } from 'node:child_process'
import { realpath, readFile, readlink, stat } from 'node:fs/promises'
import { join } from 'node:path'
import { promisify } from 'node:util'

const exec = (file:string,args:string[],options:{timeout:number;maxBuffer:number;env?:NodeJS.ProcessEnv;windowsHide?:boolean}) =>
  promisify(execFile)(file,args,{...options,encoding:'utf8'})
const hash = (value:string):string => createHash('sha256').update(value).digest('hex')
export type ExecutableIdentity = { pathHash:string; fileId:string }
export type ProcessIdentity = { pid:number; birth:string; executable:ExecutableIdentity }
export async function executableIdentity(path:string, platform:NodeJS.Platform=process.platform):Promise<ExecutableIdentity> {
  const canonical=await realpath(path), file=await stat(canonical)
  if(!file.isFile()) throw Error('EXECUTABLE_UNAVAILABLE')
  const normalized=platform==='win32'?canonical.replace(/\\/g,'/').toLowerCase():canonical
  return {pathHash:hash(normalized),fileId:`${file.dev}:${file.ino}:${file.size}:${file.mtimeMs}`}
}
export const sameProcessIdentity=(a:ProcessIdentity,b:ProcessIdentity):boolean =>
  a.pid===b.pid&&a.birth===b.birth&&a.executable.pathHash===b.executable.pathHash&&a.executable.fileId===b.executable.fileId

export const windowsQueryScript=(pid:number):string => {
  if(!Number.isSafeInteger(pid)||pid<=0) throw Error('INVALID_PID')
  return `$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); $p=Get-CimInstance Win32_Process -Filter 'ProcessId = ${pid}'; if($null -eq $p){'null'}else{if(!$p.ExecutablePath -or !$p.CreationDate){throw 'PROCESS_IDENTITY_UNAVAILABLE'}; @{pid=[int]$p.ProcessId; birth=$p.CreationDate.ToUniversalTime().Ticks.ToString(); image=$p.ExecutablePath} | ConvertTo-Json -Compress}`
}
function powershell():string { return join(process.env.SystemRoot??'C:\\Windows','System32','WindowsPowerShell','v1.0','powershell.exe') }
async function ps(script:string):Promise<string> {
  return (await exec(powershell(),['-NoProfile','-NonInteractive','-EncodedCommand',Buffer.from(script,'utf16le').toString('base64')],{timeout:5000,maxBuffer:65536,windowsHide:true})).stdout.trim()
}
async function darwinProcessSnapshot(pid:number):Promise<{birth:string;image:string}|null> {
  let output:string
  try { output=(await exec('/bin/ps',['-ww','-p',String(pid),'-o','state=','-o','lstart=','-o','comm='],{timeout:1500,maxBuffer:16384,env:{...process.env,LC_ALL:'en_US.UTF-8',TZ:'UTC'}})).stdout.trim() }
  catch(error){if((error as {code?:unknown;stdout?:string}).code===1&&!(error as {stdout?:string}).stdout?.trim())return null;throw error}
  const match=output.match(/^(\S+)\s+(.{24})\s+(.+)$/)
  if(!match)throw Error('PROCESS_IDENTITY_UNAVAILABLE')
  // A zombie has no executable image; macOS may expose only "(node)".
  if(match[1].startsWith('Z'))return null
  return {birth:match[2],image:match[3]}
}
export async function queryProcessIdentity(pid:number):Promise<ProcessIdentity|null> {
  if(!Number.isSafeInteger(pid)||pid<=0) throw Error('INVALID_PID')
  if(process.platform==='win32') {
    const value=JSON.parse(await ps(windowsQueryScript(pid))) as null|{pid:number;birth:string;image:string}
    if(value===null)return null
    if(value.pid!==pid||typeof value.birth!=='string'||!(/^[1-9]\d{0,18}$/).test(value.birth)||BigInt(value.birth)>3155378975999999999n||typeof value.image!=='string'||!value.image.trim())throw Error('PROCESS_IDENTITY_UNAVAILABLE')
    return {pid,birth:`win32:${value.birth}`,executable:await executableIdentity(value.image)}
  }
  if(process.platform==='linux') {
    try {
      const base=`/proc/${pid}`, info=await readFile(join(base,'stat'),'utf8')
      const fields=info.slice(info.lastIndexOf(')')+2).split(' ')
      const boot=(await readFile('/proc/sys/kernel/random/boot_id','utf8')).trim()
      await readlink(join(base,'exe'))
      return {pid,birth:`linux:${boot}:${fields[19]}`,executable:await executableIdentity(join(base,'exe'))}
    }catch(error){if((error as NodeJS.ErrnoException).code==='ENOENT')return null;throw error}
  }
  if(process.platform!=='darwin')throw Error('PROCESS_IDENTITY_UNSUPPORTED')
  const snapshot=await darwinProcessSnapshot(pid)
  if(!snapshot)return null
  const boot=(await exec('/usr/sbin/sysctl',['-n','kern.boottime'],{timeout:1500,maxBuffer:1024})).stdout.trim()
  try {
    return {pid,birth:`darwin:${hash(boot)}:${snapshot.birth}`,executable:await executableIdentity(snapshot.image)}
  }catch(error){
    // Only a second observed exit resolves the race. A live process with an
    // unreadable image remains an identity failure and must never be signalled.
    if((error as NodeJS.ErrnoException).code==='ENOENT' && !await darwinProcessSnapshot(pid))return null
    throw error
  }
}

// Keep a Windows kernel process handle open throughout taskkill. Checking birth
// then starting taskkill without that handle would leave a PID-reuse window.
export function windowsPinnedStopScript(identity:ProcessIdentity):string {
  const ticks=identity.birth.replace(/^win32:/,'')
  if(!Number.isSafeInteger(identity.pid)||identity.pid<=0||!/^\d+$/.test(ticks)||!/^[a-f0-9]{64}$/.test(identity.executable.pathHash))throw Error('INVALID_PROCESS_IDENTITY')
  return `$ErrorActionPreference='Stop'; Add-Type -TypeDefinition @'
using System; using System.Text; using System.Diagnostics; using System.Security.Cryptography; using System.Runtime.InteropServices;
public static class HscPinnedGameStop {
 [DllImport("kernel32.dll",SetLastError=true)] static extern IntPtr OpenProcess(uint access,bool inherit,int pid);
 [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetProcessTimes(IntPtr h,out long creation,out long exit,out long kernel,out long user);
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool QueryFullProcessImageName(IntPtr h,int flags,StringBuilder name,ref int size);
 [DllImport("kernel32.dll")] static extern uint WaitForSingleObject(IntPtr h,uint timeout);
 public static bool Stop(int pid,long birth,string imageHash) {
  IntPtr h=OpenProcess(0x100000|0x1000,false,pid); if(h==IntPtr.Zero)return false;
  try {
   long c,e,k,u; if(!GetProcessTimes(h,out c,out e,out k,out u)||DateTime.FromFileTimeUtc(c).Ticks!=birth||WaitForSingleObject(h,0)!=258)return false;
   var name=new StringBuilder(32768); int size=name.Capacity; if(!QueryFullProcessImageName(h,0,name,ref size))return false;
   string actual; using(var sha=SHA256.Create())actual=BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(name.ToString().Replace('\\','/').ToLowerInvariant()))).Replace("-","").ToLowerInvariant();
   if(actual!=imageHash)return false;
   var info=new ProcessStartInfo(System.IO.Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),"taskkill.exe"),"/pid "+pid+" /t /f"); info.UseShellExecute=false; info.CreateNoWindow=true; info.RedirectStandardOutput=true; info.RedirectStandardError=true;
   using(var killer=Process.Start(info)){if(killer==null)return false; var output=killer.StandardOutput.ReadToEndAsync(); var errors=killer.StandardError.ReadToEndAsync(); if(!killer.WaitForExit(2500))return false; return killer.ExitCode==0&&WaitForSingleObject(h,1000)==0;}
  }finally{CloseHandle(h);}
 }
}
'@; if([HscPinnedGameStop]::Stop(${identity.pid},[long]${ticks},'${identity.executable.pathHash}')){'true'}else{'false'}`
}
export async function stopPinnedWindowsProcess(identity:ProcessIdentity):Promise<boolean> {
  if(process.platform!=='win32')return false
  try{return await ps(windowsPinnedStopScript(identity))==='true'}catch{return false}
}
