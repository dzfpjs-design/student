/** Observe fresh remote PCM without replacing the browser's RTC playback/AEC.
 * This is media-path evidence, not proof that a physical speaker is audible. */
export class RemotePlaybackObservation {
  private readonly context: AudioContext
  private readonly source: MediaStreamAudioSourceNode
  private readonly analyser: AnalyserNode
  private readonly sink: GainNode
  private readonly samples: Float32Array<ArrayBuffer>
  private timer: ReturnType<typeof setTimeout> | undefined
  private deadlineTimer: ReturnType<typeof setTimeout> | undefined
  private generation = 0
  private disposed = false

  constructor(private readonly audio: HTMLAudioElement, private readonly stream: MediaStream) {
    const context = new AudioContext()
    this.context = context
    let source: MediaStreamAudioSourceNode | undefined, analyser: AnalyserNode | undefined, sink: GainNode | undefined
    try {
      this.analyser = analyser = context.createAnalyser()
      this.sink = sink = context.createGain()
      this.source = source = context.createMediaStreamSource(stream)
      analyser.fftSize = 512
      this.samples = new Float32Array(this.analyser.fftSize)
      this.sink.gain.value = 0
      this.source.connect(this.analyser)
      this.analyser.connect(this.sink)
      this.sink.connect(this.context.destination)
    } catch (error) {
      source?.disconnect(); analyser?.disconnect(); sink?.disconnect()
      void context.close().catch(() => undefined)
      throw error
    }
  }

  arm(onObserved: () => void, onUnavailable: () => void): void {
    this.disarm()
    if (this.disposed) return
    const generation = this.generation
    const owns = (): boolean => !this.disposed && this.generation === generation
    const deadline = performance.now() + 1500
    let resumeSettled = false
    const unavailable = (): void => {
      if (!owns()) return
      this.disarm()
      onUnavailable()
    }
    const observe = (startedAt: number): void => {
      if (!owns()) return
      if (this.context.state === 'closed' || !this.stream.getAudioTracks().some(track => track.readyState === 'live')) {
        unavailable(); return
      }
      // A pre-authorized sample window, primed silent stream or paused player
      // must never become the current reply's first-media receipt.
      if (this.context.state === 'running' && !this.audio.muted && !this.audio.paused
          && this.audio.readyState >= 2 && this.context.currentTime - startedAt >= this.samples.length / this.context.sampleRate) {
        this.analyser.getFloatTimeDomainData(this.samples)
        if (!this.samples.every(value => Number.isFinite(value))) { unavailable(); return }
        const rms = Math.sqrt(this.samples.reduce((sum, value) => sum + value * value, 0) / this.samples.length)
        const hasSignal = rms >= 0.001
        if (hasSignal) { this.disarm(); onObserved(); return }
      }
      this.timer = setTimeout(() => observe(startedAt), 20)
    }
    const ready = (): void => {
      if (!owns() || resumeSettled) return
      resumeSettled = true
      if (this.deadlineTimer !== undefined) clearTimeout(this.deadlineTimer)
      this.deadlineTimer = undefined
      if (this.context.state !== 'running' || performance.now() > deadline) { unavailable(); return }
      observe(this.context.currentTime)
    }
    if (this.context.state === 'running') ready()
    else {
      this.deadlineTimer = setTimeout(unavailable, 1500)
      try { void this.context.resume().then(ready, unavailable) } catch { unavailable() }
    }
  }

  disarm(): void {
    this.generation += 1
    if (this.timer !== undefined) clearTimeout(this.timer)
    if (this.deadlineTimer !== undefined) clearTimeout(this.deadlineTimer)
    this.timer = undefined; this.deadlineTimer = undefined
    this.samples.fill(0)
  }

  dispose(): void {
    if (this.disposed) return
    this.disposed = true
    this.disarm()
    this.source.disconnect(); this.analyser.disconnect(); this.sink.disconnect()
    void this.context.close().catch(() => undefined)
  }
}
