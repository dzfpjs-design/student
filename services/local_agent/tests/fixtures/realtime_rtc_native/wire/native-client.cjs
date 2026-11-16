// Disposable synthetic Runtime and local peer only; never loads credentials.
const fs = require('node:fs'), path = require('node:path'), os = require('node:os'), http = require('node:http')
const { app, BrowserWindow } = require('electron')
const input = JSON.parse(fs.readFileSync(0, 'utf8'))
if (input.runtime.sessionToken !== 'synthetic-output-session-token' || !/^http:\/\/127\.0\.0\.1:\d+$/.test(input.runtime.localApiUrl)) throw Error('synthetic only')
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'hsc-rtc-wire-'))
app.setPath('userData', profile)
app.commandLine.appendSwitch('disable-gpu')
app.commandLine.appendSwitch('no-proxy-server')
let server, window, deadline, runtimeTurnId, failureDiagnostics, runtimePhase = 'not_requested'
async function runtime(method, url, body) {
  const response = await fetch(input.runtime.localApiUrl + url, { method, signal: AbortSignal.timeout(5000),
    headers: { 'Content-Type': 'application/json', 'X-HSC-Session': input.runtime.sessionToken }, body: JSON.stringify(body) })
  if (!response.ok) throw Error('synthetic Runtime failed')
  return response.json()
}
async function main() {
  const bundle = fs.readFileSync(path.join(__dirname, 'native-protocol.js'))
  let dispatched = false
  server = http.createServer(async (request, response) => {
    response.setHeader('Cache-Control', 'no-store')
    if (request.url === '/' && request.method === 'GET') {
      response.setHeader('Content-Type', 'text/html'); response.end('<!doctype html><title>隔离语音协议验证</title><script src="/protocol.js"></script>'); return
    }
    if (request.url === '/protocol.js' && request.method === 'GET') { response.setHeader('Content-Type', 'text/javascript'); response.end(bundle); return }
    if (request.url !== '/original-runtime' || request.method !== 'POST' || dispatched) { response.writeHead(409); response.end(); return }
    dispatched = true
    try {
      const scope = input.scope
      runtimePhase = 'resume_request'
      const turn = await runtime('POST', `/v1/sessions/${scope.session_id}/voice/realtime/actions/${scope.client_action_id}/resume`, {
        child_id: scope.child_id, voice_input_id: scope.voice_input_id,
      })
      runtimePhase = 'resume_received'
      if (turn.transcript !== input.original || turn.action.type !== 'none' || turn.tool_status !== 'pending'
        || turn.action.local_only !== true || turn.action.cloud_transmission_allowed !== false) throw Error('original policy changed')
      runtimePhase = 'outcome_request'
      await runtime('PUT', `/v1/agent-turns/${turn.agent_turn_id}/outcome`, { final_response: turn.response_text, tool_status: 'completed' })
      runtimePhase = 'outcome_received'
      runtimeTurnId = turn.agent_turn_id
      response.setHeader('Content-Type', 'application/json')
      response.end(JSON.stringify({ responseText: turn.response_text, speak: false }))
    } catch { response.writeHead(500); response.end() }
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  await app.whenReady()
  window = new BrowserWindow({ show: false, webPreferences: { nodeIntegration: false, contextIsolation: true, sandbox: true } })
  await window.loadURL(`http://127.0.0.1:${server.address().port}`)
  const observed = await window.webContents.executeJavaScript(`(${qualification.toString()})(${JSON.stringify({ original: input.original, replayDeliveryDelayMs: input.replay_delivery_delay_ms ?? 0 })})`)
  if (observed.failure_diagnostics) { failureDiagnostics = { ...observed.failure_diagnostics, runtime_phase: runtimePhase }; throw Error('qualification failed') }
  if (!runtimeTurnId) throw Error('missing durable turn')
  observed.runtime_turn_id = runtimeTurnId
  observed.electron_version = process.versions.electron
  observed.chromium_version = process.versions.chrome
  return observed
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
  const wait = async predicate => {
    const deadline = performance.now() + 5000
    while (!predicate()) { if (performance.now() > deadline) throw Error('owned deadline'); await new Promise(resolve => setTimeout(resolve, 10)) }
  }
  const peers = [], sessions = [], transports = [], ownedAudio = []
  let stage = 'starting'
  let stoppedTracks = 0
  const outcome = { scenarios: [], runtime_requests: 0, response_requests: 0, complete_playback_receipts: 0,
    supplier_real: false, microphone_used: false, audio_frames: 0, peer_count: 0, closed_peers: 0 }
  async function pair(callback) {
    const a = new RTCPeerConnection({ iceServers: [] }), b = new RTCPeerConnection({ iceServers: [] })
    peers.push(a, b)
    a.onicecandidate = event => { if (event.candidate) void b.addIceCandidate(event.candidate).catch(() => {}) }
    b.onicecandidate = event => { if (event.candidate) void a.addIceCandidate(event.candidate).catch(() => {}) }
    const audio = createOwnedNativeAudio(a); ownedAudio.push(audio)
    const local = a.createDataChannel('synthetic-events'), inbound = new Promise(resolve => { b.ondatachannel = event => resolve(event.channel) })
    await a.setLocalDescription(await a.createOffer()); await b.setRemoteDescription(a.localDescription)
    await b.setLocalDescription(await b.createAnswer()); await a.setRemoteDescription(b.localDescription)
    const remote = await inbound
    await wait(() => local.readyState === 'open' && remote.readyState === 'open')
    const received = [], transcripts = []
    let sentEvents = 0, deliveredEvents = 0
    const readyNonce = crypto.randomUUID()
    let readinessAcknowledged = false
    const session = new remoteVoice.QwenRealtimeVoiceSession({ childName: 'synthetic', requireCommittedTranscript: true,
      callbacks: { onStatus: () => {}, onUserTranscript: text => transcripts.push(text), onToolCall: callback } })
    Object.assign(session, { channel: local, microphoneReleased: true, peer: a,
      audioTrack: audio.cloud, localSourceTrack: audio.root, stream: audio.stream, sender: audio.sender })
    sessions.push(session)
    local.onmessage = event => {
      const value = JSON.parse(event.data)
      if (value.type === 'synthetic.transport.ready_ack' && value.nonce === readyNonce) {
        readinessAcknowledged = true
        return
      }
      session.receive(event.data); deliveredEvents++
    }
    remote.onmessage = event => {
      const value = JSON.parse(event.data)
      if (value.type === 'synthetic.transport.ready_probe' && value.nonce === readyNonce) {
        remote.send(JSON.stringify({ type: 'synthetic.transport.ready_ack', nonce: readyNonce }))
        return
      }
      received.push(value)
    }
    const previousStage = stage
    stage = 'native_transport_handshake'
    local.send(JSON.stringify({ type: 'synthetic.transport.ready_probe', nonce: readyNonce }))
    await wait(() => readinessAcknowledged)
    outcome.transport_handshakes = (outcome.transport_handshakes ?? 0) + 1
    stage = previousStage
    const emit = (type, values = {}) => { remote.send(JSON.stringify({ type, ...values })); sentEvents++ }
    const final = (id, text) => {
      emit('input_audio_buffer.speech_started', { item_id: id })
      emit('conversation.item.input_audio_transcription.completed', { item_id: id, transcript: text })
    }
    const tool = id => {
      emit('response.created', { response: { id } })
      emit('response.function_call_arguments.done', { response_id: id, call_id: id, name: 'handle_child_turn', arguments: '{}' })
    }
    const value = { session, received, transcripts, emit, final, tool, eventCounts: () => ({ sent: sentEvents, delivered: deliveredEvents }), outputs: () => received.filter(event => event.type === 'conversation.item.create') }
    transports.push(value)
    return value
  }
  try {
    stage = 'canonical_runtime';
    let originalPassed = false, originalResult = ''
    const original = await pair(async text => {
      originalPassed = text === input.original; outcome.runtime_requests++
      const response = await fetch('/original-runtime', { method: 'POST' })
      ensure(response.ok, 'real Runtime failed')
      const result = await response.json(); originalResult = result.responseText
      return { ...result, onPlaybackEnd: () => outcome.complete_playback_receipts++ }
    })
    original.final('native-original-input', input.original); original.tool('native-original-response')
    await wait(() => original.outputs().length === 1)
    const body = JSON.parse(original.outputs()[0].item.output)
    ensure(originalPassed && originalResult.length > 0 && body.response_text === 'local_reply_no_remote_speech', 'original boundary')
    ensure(!JSON.stringify(original.received).includes(originalResult), 'local body uploaded')
    ensure(!original.received.some(event => event.type === 'response.create'), 'silent reply requested speech')
    outcome.scenarios.push('canonical_runtime_local_body_withheld')

    stage = 'abandoned_input';
    let finish
    const abandonedBody = '本地合成取消证据，不能在断言中当作公开回复。'
    const abandoned = await pair(() => new Promise(resolve => { finish = resolve }))
    abandoned.final('native-abandoned-input', '第一句话'); abandoned.tool('native-abandoned-response')
    await wait(() => typeof finish === 'function')
    abandoned.final('native-next-input', '第二句话完整保留')
    await wait(() => abandoned.transcripts.includes('第二句话完整保留'))
    finish({ responseText: abandonedBody })
    await wait(() => abandoned.outputs().length === 1)
    ensure(!JSON.stringify(abandoned.received).includes(abandonedBody), 'abandoned body uploaded')
    ensure(JSON.parse(abandoned.outputs()[0].item.output).playback_abandoned === true, 'missing abandonment')
    ensure(abandoned.session.takePendingInputsForReconnect()[0]?.utterance === '第二句话完整保留', 'pending original lost')
    outcome.scenarios.push('abandoned_body_withheld_next_original_preserved')

    stage = 'response_replay';
    const replay = await pair(async () => ({ responseText: '', speak: false }))
    replay.final('native-first-input', '旧句话'); replay.tool('native-first-response')
    await wait(() => replay.outputs().length === 1)
    replay.emit('response.done', { response: { id: 'native-first-response', status: 'completed' } })
    replay.final('native-fresh-input', '新句话不能被旧响应拿走')
    ensure(Number.isInteger(input.replayDeliveryDelayMs) && input.replayDeliveryDelayMs >= 0 && input.replayDeliveryDelayMs <= 1000, 'invalid synthetic delay')
    const replayTail = (async () => {
      if (input.replayDeliveryDelayMs) await new Promise(resolve => setTimeout(resolve, input.replayDeliveryDelayMs))
      replay.emit('response.created', { response: { id: 'native-fresh-response' } })
      replay.emit('response.created', { response: { id: 'native-first-response' } })
      replay.emit('response.done', { response: { id: 'native-first-response', status: 'completed' } })
    })()
    await wait(() => replay.transcripts.includes('新句话不能被旧响应拿走'))
    await replayTail
    await wait(() => replay.eventCounts().delivered === replay.eventCounts().sent)
    ensure(replay.session.activeResponseId === 'native-fresh-response', 'replay changed owner')
    ensure(replay.session.takePendingInputsForReconnect()[0]?.utterance === '新句话不能被旧响应拿走', 'replay consumed original')
    outcome.scenarios.push('response_replay_keeps_fresh_owner_and_input')
  } catch (error) {
    outcome.failure_diagnostics = { stage, code: error.message, runtime_requests: outcome.runtime_requests,
      source_states_at_failure: ownedAudio.map(audio => ({ root: audio.root.readyState === 'live' ? 'live' : audio.root.readyState === 'ended' ? 'ended' : 'other', cloud: audio.cloud.readyState === 'live' ? 'live' : audio.cloud.readyState === 'ended' ? 'ended' : 'other' })), event_counts: transports.map(t => t.eventCounts()),
      channel_states: sessions.map(s => s.channel?.readyState), peer_states: peers.map(p => p.connectionState),
      output_counts: transports.map(t => t.outputs().length) }
  } finally {
    try { for (const audio of ownedAudio) await audio.observeInactiveSender() } finally {
      sessions.forEach(session => session.close('synthetic_finished'))
      peers.forEach(peer => peer.close())
      await Promise.all(ownedAudio.map(audio => audio.context.close()))
    }
    outcome.peer_count = peers.length
    outcome.closed_peers = peers.filter(peer => peer.connectionState === 'closed').length
    outcome.native_track_pairs = ownedAudio.map(audio => audio.snapshot())
    stoppedTracks = outcome.native_track_pairs.reduce((sum, audio) => sum + (audio.root_ready_state === 'ended') + (audio.cloud_ready_state === 'ended'), 0)
    outcome.stopped_tracks = stoppedTracks
    outcome.response_requests = transports.reduce((sum, transport) => sum + transport.received.filter(event => event.type === 'response.create').length, 0)
  }
  if (outcome.failure_diagnostics) return outcome
  ensure(outcome.closed_peers === 6 && stoppedTracks === 6 && outcome.response_requests === 0 && outcome.native_track_pairs.length === 3 && outcome.native_track_pairs.every(audio => audio.native_root_clone_distinct && audio.root_is_stream_member && audio.native_clone_calls === 1 && audio.root_stop_calls === 1 && audio.cloud_stop_calls === 1 && audio.context_state === 'closed' && audio.media?.inactive_negotiated), 'owned native root/clone resources')
  return outcome
}
deadline = setTimeout(() => { process.stdout.write(JSON.stringify({ error: 'SYNTHETIC_NATIVE_DEADLINE' })); app.exit(2) }, 25000)
main().then(result => process.stdout.write(JSON.stringify(result)), () => {
  process.stdout.write(JSON.stringify({ error: 'SYNTHETIC_NATIVE_FAILED', diagnostics: failureDiagnostics ?? null })); process.exitCode = 2
}).finally(async () => {
  clearTimeout(deadline)
  if (window && !window.isDestroyed()) window.destroy()
  if (server) { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)) }
  try { fs.rmSync(profile, { recursive: true, force: true }) } catch { /* Only this disposable profile. */ }
  app.exit(process.exitCode || 0)
})
