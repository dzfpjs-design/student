type Options = { current: () => boolean; failed: (code: string) => void }
/** WS PCM enters a real local RTC receiver so the original remote-track AEC
 * gate remains meaningful. Neither peer ever receives a microphone track. */
export class QwenNativePcmDownlink {
  private context: AudioContext | null = null
  private destination: MediaStreamAudioDestinationNode | null = null
  private sender: RTCPeerConnection | null = null
  private receiver: RTCPeerConnection | null = null
  private sources = new Set<AudioBufferSourceNode>()
  private queuedSeconds = 0
  private nextAt = 0
  private responseId: string | null = null
  private disposed = false
  constructor(private readonly options: Options) {}
  async start(): Promise<MediaStream> {
    const context = new AudioContext({ sampleRate: 24000 })
    this.context = context
    const destination = context.createMediaStreamDestination(); this.destination = destination
    const sender = new RTCPeerConnection({ iceServers: [] }), receiver = new RTCPeerConnection({ iceServers: [] })
    this.sender = sender; this.receiver = receiver
    const current = (): boolean => !this.disposed && this.options.current() && this.sender === sender && this.receiver === receiver
    const fault = (): void => { if (current()) this.options.failed('QWEN_NATIVE_DOWNLINK_FAILED') }
    sender.addEventListener('connectionstatechange', () => { if (['failed', 'closed'].includes(sender.connectionState)) fault() })
    receiver.addEventListener('connectionstatechange', () => { if (['failed', 'closed'].includes(receiver.connectionState)) fault() })
    let finish!: (stream: MediaStream) => void
    const incoming = new Promise<MediaStream>(resolve => { finish = resolve })
    receiver.addEventListener('track', event => {
      if (!current() || event.track.kind !== 'audio') return
      event.track.addEventListener('ended', fault, { once: true })
      finish(event.streams[0] ?? new MediaStream([event.track]))
    })
    const output = destination.stream.getAudioTracks()[0]
    if (!output) throw Error('QWEN_NATIVE_DOWNLINK_TRACK_MISSING')
    output.addEventListener('ended', fault, { once: true })
    sender.addTrack(output, destination.stream)
    const gathered = async (peer: RTCPeerConnection): Promise<void> => {
      if (peer.iceGatheringState === 'complete') return
      await new Promise<void>((resolve, reject) => {
        const done = (): void => { if (peer.iceGatheringState === 'complete') { clearTimeout(timer); peer.removeEventListener('icegatheringstatechange', done); resolve() } }
        const timer = setTimeout(() => { peer.removeEventListener('icegatheringstatechange', done); reject(Error('QWEN_NATIVE_LOCAL_ICE_TIMEOUT')) }, 5000)
        peer.addEventListener('icegatheringstatechange', done)
      })
    }
    try {
      if (context.state === 'suspended') await context.resume()
      await sender.setLocalDescription(await sender.createOffer()); await gathered(sender)
      if (!current() || !sender.localDescription) throw Error('QWEN_NATIVE_DOWNLINK_RETIRED')
      await receiver.setRemoteDescription(sender.localDescription)
      await receiver.setLocalDescription(await receiver.createAnswer()); await gathered(receiver)
      if (!current() || !receiver.localDescription) throw Error('QWEN_NATIVE_DOWNLINK_RETIRED')
      await sender.setRemoteDescription(receiver.localDescription)
      const connected = async (peer: RTCPeerConnection): Promise<void> => {
        if (peer.connectionState === 'connected') return
        await new Promise<void>((resolve, reject) => {
          const changed = (): void => {
            if (peer.connectionState === 'connected') { clearTimeout(timer); peer.removeEventListener('connectionstatechange', changed); resolve() }
            else if (['failed', 'closed'].includes(peer.connectionState)) { clearTimeout(timer); peer.removeEventListener('connectionstatechange', changed); reject(Error('QWEN_NATIVE_LOCAL_PEER_FAILED')) }
          }
          const timer = setTimeout(() => { peer.removeEventListener('connectionstatechange', changed); reject(Error('QWEN_NATIVE_LOCAL_PEER_TIMEOUT')) }, 8000)
          peer.addEventListener('connectionstatechange', changed)
        })
      }
      await Promise.all([connected(sender), connected(receiver)])
      let timer: ReturnType<typeof setTimeout> | undefined
      const stream = await Promise.race([incoming, new Promise<never>((_resolve, reject) => {
        timer = setTimeout(() => reject(Error('QWEN_NATIVE_DOWNLINK_TIMEOUT')), 8000)
      })]).finally(() => { if (timer !== undefined) clearTimeout(timer) })
      if (!current()) throw Error('QWEN_NATIVE_DOWNLINK_RETIRED')
      return stream
    } catch (cause) { this.dispose(); throw cause }
  }
  begin(responseId: string,preservePrior=false): void {
    if(!preservePrior)this.cancel(); if (!this.disposed) this.responseId = responseId
  }
  append(responseId: string, encoded: string): boolean {
    const context = this.context, destination = this.destination
    if (this.disposed || !this.options.current() || responseId !== this.responseId || !context || !destination) return false
    if (typeof encoded !== 'string' || encoded.length > 262144) { this.abortResponse(); return false }
    let raw: string
    try { raw = atob(encoded) } catch { this.abortResponse(); return false }
    if (!raw.length || raw.length % 2) { this.abortResponse(); return false }
    const duration = raw.length / 2 / 24000
    if (this.queuedSeconds + duration > 90 || this.sources.size >= 16384) { this.abortResponse(); return false }
    const samples = new Float32Array(raw.length / 2)
    for (let i = 0; i < samples.length; i++) {
      const value = raw.charCodeAt(i * 2) | raw.charCodeAt(i * 2 + 1) << 8
      samples[i] = (value >= 32768 ? value - 65536 : value) / 32768
    }
    const buffer = context.createBuffer(1, samples.length, 24000); buffer.copyToChannel(samples, 0); samples.fill(0)
    const source = context.createBufferSource(); source.buffer = buffer
    source.connect(destination) // The single remoteAudio is the only audible sink.
    const epoch = this.responseId
    this.sources.add(source); this.queuedSeconds += duration
    source.onended = () => {
      source.onended = null; source.disconnect()
      if (this.sources.delete(source)) this.queuedSeconds = Math.max(0, this.queuedSeconds - duration)
      if (this.responseId !== epoch) return
    }
    this.nextAt = Math.max(this.nextAt, context.currentTime + 0.02)
    try{source.start(this.nextAt); this.nextAt += duration}catch{this.abortResponse();return false}
    return true
  }
  private abortResponse(): void { this.cancel(); this.options.failed('QWEN_NATIVE_AUDIO_QUEUE_INVALID') }
  cancel(): void {
    this.responseId = null
    for (const source of this.sources) { source.onended = null; try { source.stop() } catch { /* already ended */ } source.disconnect() }
    this.sources.clear(); this.queuedSeconds = 0; this.nextAt = 0
  }
  dispose(): void {
    if (this.disposed) return
    this.disposed = true; this.cancel()
    this.destination?.stream.getTracks().forEach(track => track.stop())
    this.sender?.close(); this.receiver?.close(); this.sender = null; this.receiver = null
    void this.context?.close().catch(() => undefined); this.context = null; this.destination = null
  }
}
