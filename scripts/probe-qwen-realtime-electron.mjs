import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { app, BrowserWindow, ipcMain } from 'electron'

const endpoint = String(process.env.HSC_QWEN_REALTIME_ENDPOINT || '').trim().replace(/\/$/, '')
const model = 'qwen3.8-omni-flash-realtime'
const keychainService = String(process.env.HSC_QWEN_REALTIME_KEYCHAIN_SERVICE || 'hsc-qwen-realtime-api-key').trim()
const fixtureDir = String(process.env.HSC_QWEN_REALTIME_FIXTURE_DIR || '').trim()
let syntheticSpeech = ''
if (fixtureDir) {
  const manifest = JSON.parse(readFileSync(join(fixtureDir, 'fixture-manifest.json'), 'utf8'))
  if (manifest.speaker !== 'adult_or_synthetic_non_child' || manifest.fixtures?.start?.file !== 'start.wav') {
    throw new Error('Realtime probe requires the approved synthetic adult fixture manifest.')
  }
  const bytes = readFileSync(join(fixtureDir, 'start.wav'))
  if (bytes.length > 256_000 || createHash('sha256').update(bytes).digest('hex') !== manifest.fixtures.start.sha256) {
    throw new Error('Realtime synthetic speech fixture failed integrity checks.')
  }
  syntheticSpeech = bytes.toString('base64')
}

if (!/^https:\/\/[a-zA-Z0-9_-]+\.(?:cn-beijing|ap-southeast-1)\.maas\.aliyuncs\.com$/.test(endpoint)) {
  console.error(JSON.stringify({ ok: false, stage: 'configuration', error: 'HSC_QWEN_REALTIME_ENDPOINT is missing or invalid.' }))
  process.exit(2)
}

app.setPath('userData', join(tmpdir(), `hsc-qwen-realtime-probe-${process.pid}`))
app.commandLine.appendSwitch('autoplay-policy', 'no-user-gesture-required')

let finished = false
let lastStage = 'main_start'
const finish = (result) => {
  if (finished) return
  finished = true
  const exitCode = result?.ok === true ? 0 : 1
  console.log(JSON.stringify(result))
  setTimeout(() => process.exit(exitCode), 100)
}

ipcMain.handle('probe:negotiate', async (_event, offerSdp) => {
  lastStage = 'signaling_request_received'
  if (typeof offerSdp !== 'string' || !offerSdp.startsWith('v=0') || !offerSdp.includes('m=audio')) {
    throw new Error('Probe did not produce a valid audio SDP offer.')
  }
  const apiKey = execFileSync('/usr/bin/security', ['find-generic-password', '-s', keychainService, '-w'], {
    encoding: 'utf8',
    maxBuffer: 8192,
  }).trim()
  const url = new URL('/api/v1/webrtc/realtime', endpoint)
  url.searchParams.set('model', model)
  const response = await fetch(url, {
    method: 'POST',
    headers: { Authorization: `Bearer ${apiKey}`, 'Content-Type': 'application/sdp' },
    body: offerSdp,
    redirect: 'error',
    signal: AbortSignal.timeout(20_000),
  })
  const answerSdp = await response.text()
  lastStage = `signaling_response_${response.status}`
  if (!response.ok) throw new Error(`Signaling HTTP ${response.status}`)
  if (!answerSdp.startsWith('v=0') || !answerSdp.includes('m=audio')) throw new Error('Invalid SDP answer')
  return answerSdp
})

ipcMain.on('probe:result', (_event, result) => finish(result))
ipcMain.on('probe:progress', (_event, stage) => { lastStage = String(stage || lastStage) })

const renderer = `
<!doctype html><meta charset="utf-8"><script>
const { ipcRenderer } = require('electron')
const syntheticSpeech = '${syntheticSpeech}'
const startedAt = performance.now()
const report = (value) => ipcRenderer.send('probe:result', value)
const progress = (stage) => ipcRenderer.send('probe:progress', stage)
const waitForIce = (peer) => peer.iceGatheringState === 'complete' ? Promise.resolve() : new Promise((resolve, reject) => {
  const timeout = setTimeout(() => reject(new Error('ICE gathering timeout')), 8000)
  peer.addEventListener('icegatheringstatechange', function changed() {
    if (peer.iceGatheringState !== 'complete') return
    clearTimeout(timeout); peer.removeEventListener('icegatheringstatechange', changed); resolve()
  })
})
;(async () => {
  progress('renderer_started')
  const audioContext = new AudioContext({ sampleRate: 16000 })
  const oscillator = audioContext.createOscillator()
  const zeroGain = audioContext.createGain()
  const destination = audioContext.createMediaStreamDestination()
  zeroGain.gain.value = 0
  oscillator.connect(zeroGain).connect(destination)
  oscillator.start()
  const track = destination.stream.getAudioTracks()[0]
  track.enabled = false

  const peer = new RTCPeerConnection({ iceServers: [] })
  progress('peer_created')
  const sender = peer.addTrack(track, destination.stream)
  await sender.replaceTrack(null)
  progress('microphone_gated')
  let activeChannel = null
  let sessionCreated = false
  let sessionUpdated = false
  let soakTimer = null
  let transcriptReceived = false
  let toolCalled = false
  let responseAudioDone = false
  let untooledOutput = false
  let remoteAudioTrack = false
  const eventTypes = []
  let speechStartedAt = 0
  const reportSpeech = (reason) => {
    if (!syntheticSpeech) return
    report({ ok: toolCalled && responseAudioDone && remoteAudioTrack && transcriptReceived && !untooledOutput,
      model: '${model}', stage: reason, syntheticAdultSpeech: true,
      stages: { sdp: true, dataChannel: true, sessionCreated, sessionUpdated, transcriptReceived, toolCalled, responseAudioDone, remoteAudioTrack, untooledOutput },
      eventTypes,
      latencyMs: Math.round(performance.now() - speechStartedAt) })
    peer.close(); oscillator.stop(); void audioContext.close()
  }
  const bindChannel = (channel) => {
    channel.addEventListener('message', async ({ data }) => {
    let event
    try { event = JSON.parse(data) } catch { return }
    if (syntheticSpeech && eventTypes.length < 40) eventTypes.push(String(event.type || 'unknown'))
    if (event.type === 'session.created' && !sessionCreated) {
      progress('session_created')
      sessionCreated = true
      activeChannel = channel
      channel.send(JSON.stringify({
        type: 'session.update',
        session: {
          modalities: ['text', 'audio'],
          instructions: syntheticSpeech
            ? 'This is a synthetic adult speech test. For every user turn, call handle_child_turn with the complete original utterance. Do not answer directly. After the tool reply, say its response_text.'
            : 'This is a silent connectivity probe. Do not speak unless the client provides speech.',
          input_audio_transcription: { model: 'qwen3-asr-flash-realtime' },
          turn_detection: { type: 'semantic_vad', threshold: 0.5, prefix_padding_ms: 500, silence_duration_ms: 800 },
          audio: {
            input: { format: { type: 'pcm', sample_rate: 16000 } },
            output: { voice: 'Tina', format: { type: 'pcm', sample_rate: 24000 } },
          },
          tools: [{
            type: 'function',
            function: {
              name: 'handle_child_turn',
              description: 'Connectivity-probe tool.',
              parameters: { type: 'object', additionalProperties: false, required: ['utterance'], properties: { utterance: { type: 'string' } } },
            },
          }],
        },
      }))
    } else if (event.type === 'session.updated' && !sessionUpdated) {
      progress('session_updated')
      sessionUpdated = true
      await sender.replaceTrack(track)
      track.enabled = true
      if (syntheticSpeech) {
        const audioBytes = Uint8Array.from(atob(syntheticSpeech), (character) => character.charCodeAt(0))
        const decoded = await audioContext.decodeAudioData(audioBytes.buffer)
        const source = audioContext.createBufferSource()
        source.buffer = decoded
        source.connect(destination)
        speechStartedAt = performance.now()
        source.start(audioContext.currentTime + 0.3)
        soakTimer = setTimeout(() => reportSpeech('speech_timeout'), 20_000)
        progress('synthetic_adult_speech_sent')
        return
      }
      progress('soak_connected')
      soakTimer = setTimeout(async () => {
        report({ ok: true, model: '${model}', stages: { sdp: true, dataChannel: true, sessionCreated, sessionUpdated, soak: true }, connectionState: peer.connectionState, latencyMs: Math.round(performance.now() - startedAt) })
        peer.close(); oscillator.stop(); await audioContext.close()
      }, 12000)
    } else if (syntheticSpeech && event.type === 'conversation.item.input_audio_transcription.completed') {
      transcriptReceived = Boolean(String(event.transcript || '').trim())
    } else if (syntheticSpeech && event.type === 'response.function_call_arguments.done' && event.name === 'handle_child_turn') {
      toolCalled = true
      channel.send(JSON.stringify({ type: 'conversation.item.create', item: {
        type: 'function_call_output', call_id: event.call_id,
        output: JSON.stringify({ response_text: '我们先看今天的学习计划。' }),
      } }))
      channel.send(JSON.stringify({ type: 'response.create' }))
    } else if (syntheticSpeech && event.type === 'response.audio.done') {
      if (!toolCalled) untooledOutput = true
      else responseAudioDone = true
    } else if (syntheticSpeech && event.type === 'response.audio_transcript.delta' && !toolCalled) {
      untooledOutput = true
    } else if (syntheticSpeech && event.type === 'response.done' && toolCalled && responseAudioDone) {
      if (soakTimer) clearTimeout(soakTimer)
      reportSpeech('tool_reply_done')
    } else if (event.type === 'error') {
      report({ ok: false, stage: sessionCreated ? 'session_update' : 'session_create', code: event.error?.code || null, error: event.error?.message || 'Realtime service returned an error.' })
    }
    })
    channel.addEventListener('open', () => { activeChannel = channel; progress('data_channel_open') })
    channel.addEventListener('error', () => report({ ok: false, stage: 'data_channel', error: 'Data channel error.' }))
    channel.addEventListener('close', () => {
      if (sessionUpdated && soakTimer !== null) report({ ok: false, stage: 'soak', connectionState: peer.connectionState, error: 'Data channel closed during soak.' })
    })
  }
  const channel = peer.createDataChannel('oai-events')
  peer.addEventListener('track', ({ track: remote }) => {
    if (remote.kind === 'audio') remoteAudioTrack = true
  })
  bindChannel(channel)
  peer.addEventListener('datachannel', ({ channel: remoteChannel }) => bindChannel(remoteChannel))
  const offer = await peer.createOffer()
  progress('offer_created')
  await peer.setLocalDescription(offer)
  await waitForIce(peer)
  progress('ice_complete')
  const answer = await ipcRenderer.invoke('probe:negotiate', peer.localDescription.sdp)
  progress('answer_received')
  await peer.setRemoteDescription({ type: 'answer', sdp: String(answer).trim().replace(/\\r?\\n/g, '\\r\\n') + '\\r\\n' })
  progress('remote_description_set')
})().catch((error) => report({ ok: false, stage: 'client', error: error?.message || String(error) }))
</script>`

await app.whenReady()
lastStage = 'app_ready'
const window = new BrowserWindow({
  show: false,
  webPreferences: { nodeIntegration: true, contextIsolation: false, sandbox: false },
})
await window.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(renderer)}`)
lastStage = 'renderer_loaded'

setTimeout(() => finish({ ok: false, stage: 'timeout', lastStage, error: 'Realtime probe did not finish within 30 seconds.' }), syntheticSpeech ? 45_000 : 30_000)
