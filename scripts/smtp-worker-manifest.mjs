import { createHash } from 'node:crypto'
import { lstatSync, readdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve, relative, sep } from 'node:path'
import { pathToFileURL } from 'node:url'

const manifestName = 'worker-manifest.json'
const forbidden = /^(?:\.env(?:\..*)?|family-data|userdata|credentials?|secrets?|backups?|diagnostics|logs|\.ssh)$/i
const forbiddenExt = /\.(?:db(?:-(?:wal|shm))?|sqlite(?:3)?(?:-(?:wal|shm))?|pem|key|dpapi|jsonl|log)$/i

function filesOf(root) {
  const result = {}
  const directories = new Set()
  for (let path=root;;) {
    if (lstatSync(path).isSymbolicLink()) throw new Error('SMTP_WORKER_LINK_FORBIDDEN')
    const parent=dirname(path)
    if (parent===path) break
    path=parent
  }
  function visit(path) {
    const info = lstatSync(path)
    if (info.isSymbolicLink()) throw new Error('SMTP_WORKER_LINK_FORBIDDEN')
    const rel = relative(root, path).split(sep).join('/')
    if (rel && (forbidden.test(rel.split('/').at(-1)) || forbiddenExt.test(rel))) throw new Error('SMTP_WORKER_PRIVATE_FILE_FORBIDDEN')
    if (info.isDirectory()) {
      if (rel) directories.add(rel)
      for (const name of readdirSync(path).sort()) visit(join(path,name))
    } else if (info.isFile()) {
      if (rel===manifestName) return
      if (info.size<1 || info.size>1024*1024*1024 || Object.keys(result).length>=4096) throw new Error('SMTP_WORKER_FILE_INVALID')
      result[rel]={size:info.size,sha256:createHash('sha256').update(readFileSync(path)).digest('hex')}
    } else throw new Error('SMTP_WORKER_FILE_INVALID')
  }
  visit(root)
  if (!result['hsc-smtp-worker.exe'] || !Object.keys(result).some(name=>name.startsWith('_internal/'))) throw new Error('SMTP_WORKER_PACKAGE_INCOMPLETE')
  const expectedDirectories = new Set()
  for (const name of Object.keys(result)) {
    const parts=name.split('/')
    for (let i=1;i<parts.length;i++) expectedDirectories.add(parts.slice(0,i).join('/'))
  }
  if (directories.size!==expectedDirectories.size || [...directories].some(name=>!expectedDirectories.has(name))) throw new Error('SMTP_WORKER_DIRECTORY_INVALID')
  return result
}

export function createSmtpWorkerManifest(root) {
  root=resolve(root)
  const files=filesOf(root)
  const temporary=join(root,`${manifestName}.tmp`)
  writeFileSync(temporary,JSON.stringify({version:1,files})+'\n',{flag:'wx'})
  renameSync(temporary,join(root,manifestName))
}

export function validateSmtpWorkerManifest(root) {
  try {
    root=resolve(root)
    const item=lstatSync(join(root,manifestName))
    if (!item.isFile() || item.isSymbolicLink() || item.size>1024*1024) throw new Error()
    const manifest=JSON.parse(readFileSync(join(root,manifestName),'utf8'))
    if (!manifest || manifest.version!==1 || !manifest.files || Object.keys(manifest).sort().join(',')!=='files,version') throw new Error()
    const actual=filesOf(root)
    if (JSON.stringify(actual)!==JSON.stringify(manifest.files)) throw new Error()
    return []
  } catch { return ['SMTP worker package missing, changed, unsafe, or incomplete'] }
}

if (process.argv[1] && import.meta.url===pathToFileURL(resolve(process.argv[1])).href) {
  createSmtpWorkerManifest(process.argv[2] || 'dist/hsc-smtp-worker')
}
