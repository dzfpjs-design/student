// Test-only build. No Electron process, OS secret storage, network or family data.
import { build } from 'esbuild'
import { fileURLToPath } from 'node:url'
import { resolve, dirname, basename, join, relative, sep } from 'node:path'
import { readFileSync, realpathSync, statSync, existsSync, writeFileSync } from 'node:fs'
import { createHash } from 'node:crypto'

const sha=b=>createHash('sha256').update(b).digest('hex')
const here=dirname(fileURLToPath(import.meta.url))
const root=realpathSync(resolve(here,'../../../../..'))
const output=resolve(process.argv[2]??'')
if(process.argv.length!==3||!statSync(dirname(output)).isDirectory()||existsSync(output)||basename(output)!=='fault-main.mjs')throw Error('SYNTHETIC_NEW_BUNDLE_OUTPUT_REQUIRED')
const result=await build({absWorkingDir:root,entryPoints:[join(here,'entry.ts')],outfile:output,write:false,
  format:'esm',platform:'node',bundle:true,metafile:true,logLevel:'silent',
  plugins:[{name:'synthetic-electron-only',setup(build){
    build.onResolve({filter:/^electron$/},()=>({path:'electron',namespace:'synthetic'}))
    build.onLoad({filter:/.*/,namespace:'synthetic'},()=>({contents:
      `export const app={getPath:()=>''};export const safeStorage={isEncryptionAvailable:()=>true,encryptString:s=>{if(globalThis.__syntheticSettingsEncryptFailure&&s.includes('"config":')&&s.includes('"catalog":'))throw Error('SYNTHETIC_SETTINGS_ENCRYPT_FAILED');return Buffer.from('SYNTHETIC-DPAPI:'+Buffer.from(s).toString('base64'))},decryptString:b=>{const s=b.toString();if(!s.startsWith('SYNTHETIC-DPAPI:'))throw Error('synthetic invalid');return Buffer.from(s.slice(16),'base64').toString()}};`,loader:'js'}))
  }}]})
const production_inputs_sha256={}
const source=realpathSync(join(root,'src'))
for(const name of Object.keys(result.metafile.inputs)){
  if(name.startsWith('synthetic:'))continue
  const path=realpathSync(resolve(root,name))
  if(path.startsWith(source+sep))production_inputs_sha256['src/'+relative(source,path).split(sep).join('/')]=sha(readFileSync(path))
}
for(const name of ['src/main/pet-audio-catalog.ts','src/main/pet-audio-settings.ts','src/shared/petAudioTariffs.ts'])
  if(!production_inputs_sha256[name])throw Error('CURRENT_MAIN_SOURCE_NOT_BUNDLED')
if(result.outputFiles.length!==1)throw Error('ONE_SYNTHETIC_BUNDLE_REQUIRED')
const bytes=result.outputFiles[0].contents
writeFileSync(output,bytes,{flag:'wx',mode:0o600})
console.log(JSON.stringify({synthetic_only:true,bundle_source:'CURRENT_CHECKOUT_TYPESCRIPT',
  production_inputs_sha256,output_sha256:sha(bytes)}))
