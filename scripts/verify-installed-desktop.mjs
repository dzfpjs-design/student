import { writeFile } from 'node:fs/promises'

const output = process.argv[2]
if (!output) throw new Error('An output path is required')
let page = null
for (let attempt = 0; attempt < 60 && !page; attempt += 1) {
  try {
    const pages = await (await fetch('http://127.0.0.1:9222/json/list')).json()
    page = pages.find(p => p.type === 'page' && p.webSocketDebuggerUrl) ?? null
  } catch {
    // The installed desktop may still be creating its renderer.
  }
  if (!page) await new Promise(resolve => setTimeout(resolve, 500))
}
if (!page) throw new Error('Installed desktop page not found')
const ws = new WebSocket(page.webSocketDebuggerUrl)
let id = 0
const pending = new Map()
ws.addEventListener('message', e => {
  const m = JSON.parse(e.data), p = pending.get(m.id)
  if (p) { clearTimeout(p.timer); pending.delete(m.id); m.error ? p.reject(new Error(m.error.message)) : p.resolve(m.result) }
})
await new Promise((resolve, reject) => { ws.addEventListener('open', resolve, {once: true}); ws.addEventListener('error', reject, {once: true}) })
async function evaluate(expression) {
  const requestId = ++id
  const result = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(requestId); reject(new Error('Desktop probe timeout')) }, 90000)
    pending.set(requestId, {resolve, reject, timer})
    ws.send(JSON.stringify({id:requestId, method:'Runtime.evaluate', params:{expression, awaitPromise:true, returnByValue:true}}))
  })
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description ?? result.exceptionDetails.text)
  return result.result.value
}
const report = {at: new Date().toISOString(), source: 'installed_desktop', raw_media_saved:false, media_uploaded:false}
try {
  report.runtime = await evaluate(`(async()=>{const r=await window.desktop.getRuntimeConfig(); const h=await fetch(r.localApiUrl+'/v1/health',{headers:{'X-HSC-Session':r.sessionToken}}); return {status:r.status,health_http:h.status,health:await h.json()}})()`)
  report.models = await evaluate('window.desktop.getModelSettings()')
  report.voice = await evaluate('window.desktop.getVoiceSettings()')
  report.diagnostics = await evaluate('window.desktop.getDiagnosticSummary()')
  report.auto_launch = await evaluate('window.desktop.getAutoLaunch()')
  report.ui = await evaluate(`({title:document.title,text:document.body.innerText.slice(0,1600),buttons:[...document.querySelectorAll('button')].map(b=>b.innerText||b.getAttribute('aria-label'))})`)
  report.devices = await evaluate(`(async()=>{const ds=await navigator.mediaDevices.enumerateDevices();return ds.map(d=>({kind:d.kind,label:d.label}))})()`)
  report.capture = await evaluate(`(async()=>{
    const devices=await navigator.mediaDevices.enumerateDevices(); const results=[];
    for(const d of devices.filter(x=>x.kind==='videoinput'||(x.kind==='audioinput'&&!['default','communications'].includes(x.deviceId)))) {
      let stream,ctx,video;
      try {
        stream=await navigator.mediaDevices.getUserMedia(d.kind==='videoinput'?{video:{deviceId:{exact:d.deviceId}},audio:false}:{video:false,audio:{deviceId:{exact:d.deviceId},echoCancellation:true,noiseSuppression:true,autoGainControl:true}});
        const track=stream.getTracks()[0]; const settings=track.getSettings();
        const r={label:d.label,kind:d.kind,state:track.readyState};
        if(d.kind==='videoinput'){
          video=document.createElement('video'); video.muted=true; video.srcObject=stream; await video.play();
          await new Promise(resolve=>setTimeout(resolve,1500));
          const c=document.createElement('canvas');c.width=64;c.height=48;const x=c.getContext('2d');x.drawImage(video,0,0,64,48);const a=x.getImageData(0,0,64,48).data;let sum=0,sq=0,n=0;
          for(let i=0;i<a.length;i+=4){const v=(a[i]+a[i+1]+a[i+2])/3;sum+=v;sq+=v*v;n++}
          Object.assign(r,{width:video.videoWidth,height:video.videoHeight,frameRate:settings.frameRate,pixelMean:sum/n,pixelVariance:sq/n-(sum/n)**2});
        } else {
          ctx=new AudioContext();await ctx.resume();const source=ctx.createMediaStreamSource(stream),analyser=ctx.createAnalyser();analyser.fftSize=2048;source.connect(analyser);
          const buffer=new Float32Array(analyser.fftSize);let peak=0,sum=0,n=0;
          for(let k=0;k<20;k++){await new Promise(resolve=>setTimeout(resolve,100));analyser.getFloatTimeDomainData(buffer);for(const v of buffer){peak=Math.max(peak,Math.abs(v));sum+=v*v;n++}}
          Object.assign(r,{peak,rms:Math.sqrt(sum/n),echoCancellation:settings.echoCancellation,noiseSuppression:settings.noiseSuppression,sampleRate:settings.sampleRate});
        }
        results.push(r);
      }catch(e){results.push({label:d.label,kind:d.kind,error:e.name})}
      finally {if(video){video.pause();video.srcObject=null}stream?.getTracks().forEach(t=>t.stop());if(ctx)await ctx.close()}
    }return results;
  })()`)
  report.status = report.runtime.status === 'ready' && report.runtime.health_http === 200 ? 'runtime_passed_device_results_require_review' : 'failed'
} catch (error) { report.status='failed'; report.error=error.message; process.exitCode=1 }
finally { ws.close(); await writeFile(output,JSON.stringify(report,null,2)); console.log(JSON.stringify(report)) }
