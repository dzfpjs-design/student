import { spawnSync } from 'node:child_process'
const [python,exe,output] = process.argv.slice(2)
if (!output) throw new Error('python, installed EXE and output are required')
const pages=await(await fetch('http://127.0.0.1:9222/json/list')).json()
const page=pages.find(p=>p.type==='page'&&p.url.startsWith('file:'))
if(!page)throw new Error('Installed desktop is not ready')
const ws=new WebSocket(page.webSocketDebuggerUrl)
try {
  await new Promise((ok,no)=>{ws.addEventListener('open',ok,{once:true});ws.addEventListener('error',no,{once:true})})
  const frame=await new Promise((ok,no)=>{
    const timer=setTimeout(()=>no(new Error('Camera capture timeout')),20000)
    ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.id===1){clearTimeout(timer);m.result?.exceptionDetails?no(new Error('Camera capture failed')):ok(m.result?.result?.value)}}
    ws.send(JSON.stringify({id:1,method:'Runtime.evaluate',params:{awaitPromise:true,returnByValue:true,expression:`(async()=>{
      const runtime=await window.desktop.getRuntimeConfig(),headers={'X-HSC-Session':runtime.sessionToken};
      const bindings=await(await fetch(runtime.localApiUrl+'/v1/device-bindings',{headers})).json();
      const binding=bindings.items.find(x=>x.device_type==='document_camera');
      const devices=await navigator.mediaDevices.enumerateDevices();const device=devices.find(x=>x.kind==='videoinput'&&x.label===binding?.label);
      if(!device)throw Error('Bound document camera absent');let stream,video;
      try{stream=await navigator.mediaDevices.getUserMedia({audio:false,video:{deviceId:{exact:device.deviceId},width:{ideal:3840},height:{ideal:2160}}});
        const track=stream.getVideoTracks()[0];const modes=track.getCapabilities().focusMode??[];const mode=modes.includes('continuous')?'continuous':modes.includes('single-shot')?'single-shot':null;
        if(mode)await track.applyConstraints({advanced:[{focusMode:mode}]});
        video=document.createElement('video');video.muted=true;video.srcObject=stream;await video.play();await new Promise(resolve=>setTimeout(resolve,2200));
        const canvas=document.createElement('canvas');canvas.width=video.videoWidth;canvas.height=video.videoHeight;canvas.getContext('2d').drawImage(video,0,0);
        return {label:device.label,width:canvas.width,height:canvas.height,focus_mode:track.getSettings().focusMode,image:canvas.toDataURL('image/jpeg',0.9).split(',')[1]};
      }finally{video?.pause();if(video)video.srcObject=null;stream?.getTracks().forEach(t=>t.stop())}
    })()`}}))
  })
  if(!frame?.image)throw new Error('No frame')
  console.log(JSON.stringify({label:frame.label,width:frame.width,height:frame.height,focus_mode:frame.focus_mode,raw_frame_saved:false}))
  const result=spawnSync(python,[new URL('./verify-installed-document.py',import.meta.url).pathname.replace(/^\/([A-Za-z]:)/,'$1'),exe,output],{input:frame.image,encoding:'utf8',timeout:240000,maxBuffer:100000})
  console.log(result.stdout.trim())
  if(result.error||result.status!==0)throw new Error('Isolated installed OCR check failed; inspect the local summary')
} finally {ws.close()}
