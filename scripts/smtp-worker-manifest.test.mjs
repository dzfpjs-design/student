import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { createSmtpWorkerManifest, validateSmtpWorkerManifest } from './smtp-worker-manifest.mjs'

function fixture(t) {
  const base=mkdtempSync(join(realpathSync(tmpdir()),'hsc-worker-manifest-'))
  t.after(()=>rmSync(base,{recursive:true,force:true}))
  const root=join(base,'worker');mkdirSync(join(root,'_internal'),{recursive:true})
  writeFileSync(join(root,'hsc-smtp-worker.exe'),'synthetic-exe')
  writeFileSync(join(root,'_internal','python.dll'),'synthetic-dll')
  return {base,root}
}

test('manifest binds the complete tree and catches later changes',t=>{
  const {root}=fixture(t)
  createSmtpWorkerManifest(root)
  assert.deepEqual(validateSmtpWorkerManifest(root),[])
  writeFileSync(join(root,'_internal','python.dll'),'changed')
  assert.ok(validateSmtpWorkerManifest(root).length)
})

test('rejects private files, missing dependencies and symlinked ancestors',t=>{
  const {base,root}=fixture(t)
  writeFileSync(join(root,'_internal','credentials.key'),'synthetic')
  assert.throws(()=>createSmtpWorkerManifest(root))
  rmSync(join(root,'_internal','credentials.key'))
  const link=join(base,'linked')
  try { symlinkSync(root,link,process.platform==='win32'?'junction':'dir') }
  catch { return }
  assert.throws(()=>createSmtpWorkerManifest(link))
  rmSync(join(root,'_internal','python.dll'))
  assert.throws(()=>createSmtpWorkerManifest(root))
})

test('rejects unlisted empty directories and nested links',t=>{
  const {base,root}=fixture(t)
  const empty=join(root,'_internal','unlisted'); mkdirSync(empty)
  assert.throws(()=>createSmtpWorkerManifest(root),/SMTP_WORKER_DIRECTORY_INVALID/)
  rmSync(empty,{recursive:true})
  const external=join(base,'external');mkdirSync(external)
  const nested=join(root,'_internal','linked')
  try { symlinkSync(external,nested,process.platform==='win32'?'junction':'dir') }
  catch { return }
  assert.throws(()=>createSmtpWorkerManifest(root),/SMTP_WORKER_LINK_FORBIDDEN/)
})

test('legacy single-agent update entry points stop before staging or replacement',()=>{
  for (const name of ['deploy-windows-agent-hotfix.ps1','build-offline-update.ps1','build-offline-update-iexpress.ps1']) {
    const script=readFileSync(new URL(`./${name}`,import.meta.url),'utf8')
    const guard=script.indexOf("throw 'SINGLE_FILE_AGENT_UPDATE_DISABLED_USE_FULL_INSTALLER'")
    assert.ok(guard>=0,name)
    for (const action of ['Copy-Item','New-Item','Start-Process']) {
      const position=script.indexOf(action)
      if(position>=0)assert.ok(guard<position,`${name}: ${action}`)
    }
  }
})
