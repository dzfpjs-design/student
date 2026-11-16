const fs=require('node:fs');const path=require('node:path');const Module=require('node:module')
const directory=process.argv[3], mode=process.argv[2];let failDirectorySync=mode==='sync-fail'
const orig=Module._load;Module._load=function(name,...args){
 if(name==='electron')return {safeStorage:{isEncryptionAvailable:()=>true,encryptString:s=>Buffer.from(s).reverse(),decryptString:b=>Buffer.from(b).reverse().toString()}}
 if(name==='node:fs/promises' && mode==='sync-fail'){
  const actual=orig.call(this,name,...args)
  return {...actual,open:async(...openArgs)=>{const h=await actual.open(...openArgs);if(openArgs[0]===directory&&failDirectorySync&&fs.existsSync(path.join(directory,'tutor-explanation-settings.lock'))){failDirectorySync=false;return {sync:async()=>{throw Error('synthetic directory sync failure')},close:()=>h.close()}}return h}}
 }
 return orig.call(this,name,...args)
}
const settings=require(process.env.HSC_SYNTHETIC_BUNDLE)
async function run(){
 if(mode==='read'||mode==='sync-fail'){
  try{const v=await settings.loadTutorExplanationConfig(directory);process.send({ok:true,baseUrl:v?.baseUrl??null})}catch(e){process.send({ok:false,error:String(e.message)})};return
 }
 let n=0
 try {
  await settings.saveTutorExplanationConfig(directory,{apiKey:'',baseUrl:'https://new.example.test/v1',model:'deepseek-test'},async()=>{
   n++
   if(mode==='paused'&&n===2){process.send({event:'pending'});await new Promise(resolve=>process.once('message',resolve))}
   if(mode==='paused'&&n===3)process.exit(77)
   if(mode==='io-failure'&&n===3){fs.chmodSync(directory,0o500);throw Error('synthetic parent expiry')}
  })
  process.send({ok:true})
 } catch(e){process.send({ok:false,error:String(e.message)})}
}
run().then(()=>process.exit(0)).catch(()=>process.exit(2))
