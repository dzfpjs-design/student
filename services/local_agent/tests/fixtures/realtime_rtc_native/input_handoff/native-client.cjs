// Local synthetic RTC and disposable Runtime only; no supplier or credentials.
const fs = require('node:fs'), path = require('node:path'), os = require('node:os'), http = require('node:http')
const { app, BrowserWindow } = require('electron')
const input = JSON.parse(fs.readFileSync(0, 'utf8'))
if (input.runtime.sessionToken !== 'synthetic-output-session-token' || !/^http:\/\/127\.0\.0\.1:\d+$/.test(input.runtime.localApiUrl)) throw Error('synthetic only')
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'hsc-rtc-handoff-'))
app.setPath('userData', profile); app.commandLine.appendSwitch('disable-gpu'); app.commandLine.appendSwitch('no-proxy-server')
let server, window, deadline, record, turnId, started = false, executed = false
async function runtime(method, url, body) {
  const response = await fetch(input.runtime.localApiUrl + url, { method, signal: AbortSignal.timeout(5000),
    headers: { 'Content-Type': 'application/json', 'X-HSC-Session': input.runtime.sessionToken }, body: JSON.stringify(body) })
  if (!response.ok) throw Error('synthetic Runtime failed')
  return response.json()
}
async function main() {
  const bundle = fs.readFileSync(path.join(__dirname, 'native-protocol.js'))
  server = http.createServer(async (request, response) => {
    if (request.method === 'GET' && request.url === '/') { response.setHeader('Content-Type', 'text/html'); response.end('<!doctype html><title>隔离原话交接验证</title><script src="/protocol.js"></script>'); return }
    if (request.method === 'GET' && request.url === '/protocol.js') { response.setHeader('Content-Type', 'text/javascript'); response.end(bundle); return }
    const chunks = []; for await (const chunk of request) chunks.push(chunk)
    try {
      const body = JSON.parse(Buffer.concat(chunks).toString()), scope = input.scope
      if (body.utterance !== input.original || body.capture.sessionId !== scope.session_id
          || body.capture.reference.child_id !== scope.child_id || body.capture.source !== 'accepted_onset') throw Error('original changed')
      const root = `/v1/sessions/${scope.session_id}/voice/realtime`, id = body.capture.localInputId
      if (request.method === 'POST' && request.url === '/admit' && !started) {
        started = true
        await runtime('POST', root + '/begin', { client_action_id: id, model_label: 'synthetic-native-rtc',
          captured_at_ms: body.capture.capturedAt, capture_reference: body.capture.reference })
        record = await runtime('POST', root + '/complete', { client_action_id: id, transcript: body.utterance })
        if (record.original_transcript !== input.original || record.client_action_id !== id || record.status !== 'completed') throw Error('receipt changed')
        // Deliberately lose the HTTP acknowledgement after the actual commit.
        response.writeHead(503); response.end(); return
      }
      if (request.method === 'POST' && request.url === '/resume' && record && !executed && record.client_action_id === id) {
        executed = true
        const turn = await runtime('POST', `${root}/actions/${id}/resume`, { child_id: scope.child_id, voice_input_id: record.voice_input_id })
        if (turn.transcript !== input.original || turn.action.type !== 'none' || turn.action.local_only !== true) throw Error('original Runtime changed')
        await runtime('PUT', `/v1/agent-turns/${turn.agent_turn_id}/outcome`, { final_response: turn.response_text, tool_status: 'completed' })
        turnId = turn.agent_turn_id; response.writeHead(204); response.end(); return
      }
      response.writeHead(409); response.end()
    } catch { response.writeHead(500); response.end() }
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve)); await app.whenReady()
  window = new BrowserWindow({ show: false, webPreferences: { nodeIntegration: false, contextIsolation: true, sandbox: true } })
  await window.loadURL(`http://127.0.0.1:${server.address().port}`)
  const observed = await window.webContents.executeJavaScript(`(${qualification.toString()})(${JSON.stringify({ original: input.original, scope: input.scope })})`)
  if (!turnId || !record) throw Error('missing durable result')
  return { ...observed, runtime_turn_id: turnId, capture_action_id: record.client_action_id, voice_input_id: record.voice_input_id,
    electron_version: process.versions.electron, chromium_version: process.versions.chrome }
}
async function qualification(input) {
function createOwnedNativeAudio(peer) {
  const context = new AudioContext(), destination = context.createMediaStreamDestination(), stream = destination.stream
  const root = stream.getAudioTracks()[0], nativeClone = root.clone
  const cloud = Reflect.apply(nativeClone, root, [])
  if (!(root instanceof MediaStreamTrack) || !(cloud instanceof MediaStreamTrack) || root === cloud
      || stream.getAudioTracks().length !== 1 || stream.getAudioTracks()[0] !== root) throw Error('EXACT_NATIVE_ROOT_CLONE_REQUIRED')
  const transceiver = peer.addTransceiver(cloud, { direction: 'inactive', streams: [stream] }), sender = transceiver.sender
  if (sender.track !== cloud || transceiver.direction !== 'inactive') throw Error('EXACT_INACTIVE_NATIVE_SENDER_REQUIRED')
  const stops = new Map([[root, 0], [cloud, 0]])
  for (const track of [root, cloud]) {
    const original = track.stop
    Object.defineProperty(track, 'stop', { value: () => { stops.set(track, stops.get(track) + 1); return Reflect.apply(original, track, []) } })
  }
  let mediaProof = null
  const observeInactiveSender = async () => {
    if (mediaProof) return mediaProof
    if (transceiver.direction !== 'inactive' || transceiver.currentDirection !== 'inactive'
        || (sender.track !== cloud && sender.track !== null)) throw Error('INACTIVE_SENDER_SCOPE_CHANGED')
    const outbound = []
    let unknown = false
    try { const reports = await sender.getStats(); reports.forEach(row => {
      if (row.type === 'outbound-rtp' && (row.kind === 'audio' || row.mediaType === 'audio')) {
        if (!Number.isFinite(row.packetsSent) || !Number.isFinite(row.bytesSent)) unknown = true
        else outbound.push({ packets_sent: row.packetsSent, bytes_sent: row.bytesSent })
      }
    }) } catch { unknown = true }
    const observed = !unknown && outbound.length > 0
    return mediaProof = { inactive_negotiated: true, root_enabled: root.enabled, cloud_enabled: cloud.enabled,
      observation: observed ? 'observed' : 'unknown', audio_outbound_stats_count: outbound.length,
      audio_packets_sent: observed ? outbound.reduce((sum, row) => sum + row.packets_sent, 0) : null,
      audio_bytes_sent: observed ? outbound.reduce((sum, row) => sum + row.bytes_sent, 0) : null }

  }
  const snapshot = () => ({ root_id: root.id, cloud_id: cloud.id, native_root_clone_distinct: root !== cloud,
    root_is_stream_member: stream.getAudioTracks().length === 1 && stream.getAudioTracks()[0] === root,
    native_clone_calls: 1, root_ready_state: root.readyState, cloud_ready_state: cloud.readyState,
    root_stop_calls: stops.get(root), cloud_stop_calls: stops.get(cloud), context_state: context.state, media: mediaProof })
  return { context, stream, root, cloud, sender, transceiver, observeInactiveSender, snapshot }
}

  const ensure = (value, message) => { if (!value) throw Error(message) }
  const wait = async predicate => { const end = performance.now() + 6000; while (!predicate()) {
    if (performance.now() > end) throw Error('owned deadline'); await new Promise(resolve => setTimeout(resolve, 10))
  } }
  const peers = [], sessions = [], sent = [], failures = [], states = [], ownedAudio = []
  let stopped = 0, admission, resumed = false, toolCalls = 0
  const pair = async callbacks => {
    const a = new RTCPeerConnection({ iceServers: [] }), b = new RTCPeerConnection({ iceServers: [] }); peers.push(a, b)
    a.onicecandidate = e => { if (e.candidate) void b.addIceCandidate(e.candidate).catch(() => {}) }
    b.onicecandidate = e => { if (e.candidate) void a.addIceCandidate(e.candidate).catch(() => {}) }
    const audio = createOwnedNativeAudio(a); ownedAudio.push(audio)
    const local = a.createDataChannel('synthetic'), inbound = new Promise(resolve => { b.ondatachannel = e => resolve(e.channel) })
    await a.setLocalDescription(await a.createOffer()); await b.setRemoteDescription(a.localDescription)
    await b.setLocalDescription(await b.createAnswer()); await a.setRemoteDescription(b.localDescription)
    const remote = await inbound; await wait(() => local.readyState === 'open' && remote.readyState === 'open')
    const session = new remoteVoice.QwenRealtimeVoiceSession({ childName: 'synthetic', requireCommittedTranscript: true,
      callbacks: { onStatus: status => states.push(status.phase), onError: message => failures.push(message), ...callbacks } })
    Object.assign(session, { channel: local, microphoneReleased: true, peer: a,
      audioTrack: audio.cloud, localSourceTrack: audio.root, stream: audio.stream, sender: audio.sender })
    sessions.push(session); local.onmessage = e => session.receive(e.data); remote.onmessage = e => sent.push(JSON.parse(e.data))
    return { session, audio, emit: (type, value = {}) => remote.send(JSON.stringify({ type, ...value })) }
  }
  let originalCapture
  try {
    const old = await pair({ captureInputContext: () => ({ sessionId: input.scope.session_id, carryoverConfirmationId: null, reference: { child_id: input.scope.child_id } }),
      onInputCompleted: (utterance, capture) => {
        originalCapture = structuredClone(capture)
        admission = fetch('/admit', { method: 'POST', body: JSON.stringify({ utterance, capture }) }).then(response => response.status)
        throw Error('synthetic callback failure with private detail')
      }, onToolCall: async () => { toolCalls++; return { responseText: '', speak: false } } })
    old.emit('input_audio_buffer.speech_started', { item_id: 'native-handoff-original' })
    old.emit('conversation.item.input_audio_transcription.completed', { item_id: 'native-handoff-original', transcript: input.original })
    await wait(() => failures.length === 1)
    ensure(await admission === 503, 'lost acknowledgement not injected')
    old.emit('response.created', { response: { id: 'after-failure' } })
    old.emit('response.function_call_arguments.done', { response_id: 'after-failure', call_id: 'after-failure', name: 'handle_child_turn', arguments: '{}' })
    await new Promise(resolve => setTimeout(resolve, 30))
    const pending = old.session.takePendingInputsForReconnect()
    ensure(pending.length === 1 && pending[0].utterance === input.original && JSON.stringify(pending[0].capture) === JSON.stringify(originalCapture), 'frozen original lost')
    ensure(old.session.audioTrack.enabled === false && toolCalls === 0 && states.at(-1) === 'error', 'failed connection still running')
    ensure(!failures.some(message => message.includes('private detail')), 'callback detail exposed')
    await old.audio.observeInactiveSender()
    old.session.close('transfer-original')
    const next = await pair({ onUntooledTurn: async (utterance, reason, capture) => {
      ensure(reason === 'reconnect' && JSON.stringify(capture) === JSON.stringify(originalCapture), 'source rebound')
      const response = await fetch('/resume', { method: 'POST', body: JSON.stringify({ utterance, capture }) })
      ensure(response.status === 204, 'Runtime resume failed'); resumed = true
    } })
    Object.assign(next.session, { microphoneReleased: false }); next.session.resumePendingInputs(pending)
    next.emit('session.updated'); await wait(() => resumed)
    ensure(!sent.some(event => event.type === 'response.create'), 'cloud output requested')
  } finally { try { for (const audio of ownedAudio) await audio.observeInactiveSender() } finally { sessions.forEach(session => session.close()); peers.forEach(peer => peer.close()); await Promise.all(ownedAudio.map(audio => audio.context.close())) } }
  const nativePairs = ownedAudio.map(audio => audio.snapshot()); stopped = nativePairs.reduce((sum, audio) => sum + (audio.root_ready_state === 'ended') + (audio.cloud_ready_state === 'ended'), 0)
  ensure(peers.length === 4 && peers.every(peer => peer.connectionState === 'closed') && stopped === 4 && nativePairs.length === 2 && nativePairs.every(audio => audio.native_root_clone_distinct && audio.root_is_stream_member && audio.native_clone_calls === 1 && audio.root_stop_calls === 1 && audio.cloud_stop_calls === 1 && audio.context_state === 'closed' && audio.media?.inactive_negotiated), 'native resources not released')
  return { original_preserved: true, capture_preserved: true, acknowledgement_lost_after_commit: true, callback_failure_reported: true,
    runtime_resumes: 1, failed_connection_tool_calls: toolCalls, closed_peers: peers.length, stopped_tracks: stopped, native_track_pairs: nativePairs,
    supplier_real: false, microphone_used: false, paid_requests: 0, complete_playback_receipts: 0 }
}
deadline = setTimeout(() => { process.stdout.write(JSON.stringify({ error: 'SYNTHETIC_HANDOFF_DEADLINE' })); app.exit(2) }, 25000)
main().then(result => process.stdout.write(JSON.stringify(result)), () => { process.stdout.write(JSON.stringify({ error: 'SYNTHETIC_HANDOFF_FAILED' })); process.exitCode = 2 })
  .finally(async () => { clearTimeout(deadline); if (window && !window.isDestroyed()) window.destroy()
    if (server) { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)) }
    try { fs.rmSync(profile, { recursive: true, force: true }) } catch { /* Only disposable owned profile. */ }
    app.exit(process.exitCode || 0) })
