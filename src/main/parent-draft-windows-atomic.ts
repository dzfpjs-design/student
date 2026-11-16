import * as fs from 'node:fs'
import { createHash, randomUUID } from 'node:crypto'
import { basename, dirname, join, resolve } from 'node:path'
import { spawnSync } from 'node:child_process'

// No user text, tokens or keys enter this process. The two paths come only from
// the native store. WRITE_THROUGH is used without copy/delete or reboot flags.
export const windowsDraftCommitSource = `using System;
using System.IO;
using System.Runtime.InteropServices;
public static class HscDraftCommit {
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
  [return: MarshalAs(UnmanagedType.Bool)]
  static extern bool MoveFileExW(string source, string target, uint flags);
  public static int Main(string[] args) {
    try {
      if(args.Length!=2 || !Path.IsPathRooted(args[0]) || !Path.IsPathRooted(args[1])
        || !String.Equals(Path.GetDirectoryName(Path.GetFullPath(args[0])),Path.GetDirectoryName(Path.GetFullPath(args[1])),StringComparison.OrdinalIgnoreCase)) return 2;
      if(!MoveFileExW(args[0],args[1],0x1|0x8)) return 3;
      using(var file=new FileStream(args[1],FileMode.Open,FileAccess.ReadWrite,FileShare.Read,4096,FileOptions.WriteThrough)) { file.Flush(true); }
      Console.Write("HSC_DRAFT_REPLACED"); return 0;
    } catch { return 4; }
  }
}`

const digest = (bytes: Buffer | string): string => createHash('sha256').update(bytes).digest('hex')
const fail = (): Error => Error('DRAFT_RECOVERY_IO')
const samePath = (a: string, b: string): boolean => resolve(a).toLowerCase() === resolve(b).toLowerCase()
function regular(path: string): void {
  const info = fs.lstatSync(path)
  if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1) throw fail()
}
function directory(path: string): void {
  const info = fs.lstatSync(path)
  if (!info.isDirectory() || info.isSymbolicLink() || !samePath(fs.realpathSync(path), path)) throw fail()
}

/** Only OS metadata reaches these fixed native helper children. Never inherit app secrets. */
export function windowsDraftHelperEnvironment(): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {}
  for (const name of ['SystemRoot', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH']) {
    const original = Object.keys(process.env).find(key => key.toLowerCase() === name.toLowerCase())
    if (original !== undefined) env[name] = process.env[original]
  }
  return env
}

export type WindowsDraftCommitPort = { readonly root:string; assertAvailable():void; replaceAndFlush(tempBasename:string,finalBasename:string):void }

/** Windows OS commit adapter. Physical sudden-power-loss testing is separate. */
export function createWindowsDraftAtomicIO(storeDirectory: string, helperDirectory: string, port?: WindowsDraftCommitPort): { replaceAndFlush(tempPath: string, finalPath: string): void } {
  if (process.platform !== 'win32') throw fail()
  const root = resolve(storeDirectory), helperRoot = resolve(helperDirectory)
  fs.mkdirSync(helperRoot, { recursive: true, mode: 0o700 }); directory(helperRoot)
  if (port !== undefined) {
    if (!samePath(port.root, root)) throw fail()
    port.assertAvailable()
    return { replaceAndFlush(tempPath:string,finalPath:string):void {
      try {
        directory(root); directory(helperRoot)
        if (!samePath(dirname(tempPath), root) || !samePath(dirname(finalPath), root) || samePath(tempPath, finalPath)) throw fail()
        regular(tempPath)
        if (fs.existsSync(finalPath)) regular(finalPath)
        port.assertAvailable()
        port.replaceAndFlush(basename(tempPath),basename(finalPath))
      } catch { throw fail() }
    } }
  }
  const windowsRoot = process.env.SystemRoot
  if (!windowsRoot || !samePath(windowsRoot, fs.realpathSync(windowsRoot))) throw fail()
  const compiler = join(windowsRoot, 'Microsoft.NET', 'Framework64', 'v4.0.30319', 'csc.exe')
  // Protected Windows system binaries may have intrinsic WinSxS hardlinks.
  // Caller-owned helper files remain subject to regular() and their code hash.
  const compilerInfo = fs.lstatSync(compiler)
  if (!compilerInfo.isFile() || compilerInfo.isSymbolicLink() || !samePath(fs.realpathSync(compiler), compiler)) throw fail()
  const id = randomUUID(), sourcePath = join(helperRoot, `commit-${id}.cs`), executable = join(helperRoot, `commit-${id}.exe`)
  let codeHash: string
  try {
    fs.writeFileSync(sourcePath, windowsDraftCommitSource, { flag: 'wx', mode: 0o600 })
    const compiled = spawnSync(compiler, ['/nologo', '/target:exe', '/optimize+', `/out:${executable}`, sourcePath], {
      timeout: 10_000, windowsHide: true, maxBuffer: 8192, encoding: 'utf8', shell: false, env: windowsDraftHelperEnvironment()
    })
    if (compiled.error || compiled.signal || compiled.status !== 0) throw fail()
    regular(executable); codeHash = digest(fs.readFileSync(executable))
  } catch { try { fs.unlinkSync(executable) } catch { /* no executable was admitted */ } throw fail() }
  finally { try { fs.unlinkSync(sourcePath) } catch { /* no private data in source */ } }
  return {
    replaceAndFlush(tempPath: string, finalPath: string): void {
      try {
        directory(root); directory(helperRoot)
        if (!samePath(dirname(tempPath), root) || !samePath(dirname(finalPath), root) || samePath(tempPath, finalPath)) throw fail()
        regular(tempPath)
        if (fs.existsSync(finalPath)) regular(finalPath)
        regular(executable)
        if (digest(fs.readFileSync(executable)) !== codeHash) throw fail()
        const committed = spawnSync(executable, [resolve(tempPath), resolve(finalPath)], {
          timeout: 2000, windowsHide: true, maxBuffer: 1024, encoding: 'utf8', shell: false, env: windowsDraftHelperEnvironment()
        })
        if (committed.error || committed.signal || committed.status !== 0 || committed.stdout !== 'HSC_DRAFT_REPLACED' || committed.stderr) throw fail()
      } catch { throw fail() }
    }
  }
}
