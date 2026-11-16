export type MicrophoneEchoMode = 'all' | 'remote-only' | 'browser-selected' | 'disabled' | 'unknown'
export type MicrophoneEchoPolicy = {
  mode: MicrophoneEchoMode
  negotiation: 'not_supported' | 'accepted' | 'rejected' | 'timed_out' | 'ended'
  acoustic_quality_verified: false
}

export function microphoneEchoMode(track: MediaStreamTrack | null): MicrophoneEchoMode {
  try {
    const value: unknown = track?.getSettings?.().echoCancellation
    if (value === 'all' || value === 'remote-only') return value
    if (value === true) return 'browser-selected'
    if (value === false) return 'disabled'
  } catch { /* Disconnected drivers may no longer expose settings. */ }
  return 'unknown'
}

export function microphoneEchoAllowsDuplex(track: MediaStreamTrack | null, externalPlayback = false): boolean {
  const mode = microphoneEchoMode(track)
  // UA-selected true guarantees only remote-track scope, not local TTS/WebAudio.
  // Keep remote conversation duplex without mistaking it for system-wide AEC.
  return mode === 'all' || (!externalPlayback && (mode === 'browser-selected' || mode === 'remote-only'))
}

/** Optional driver negotiation must never strand the listening lifecycle. */
export async function negotiateMicrophoneEchoCancellation(track: MediaStreamTrack): Promise<MicrophoneEchoPolicy> {
  const ended = (): boolean => track.readyState === 'ended'
  const report = (negotiation: MicrophoneEchoPolicy['negotiation']): MicrophoneEchoPolicy => ({
    mode: microphoneEchoMode(track), negotiation, acoustic_quality_verified: false,
  })
  if (ended()) return report('ended')
  let supported: unknown
  try { supported = track.getCapabilities?.().echoCancellation } catch { return report('not_supported') }
  if (!Array.isArray(supported) || !supported.includes('all') || !track.applyConstraints) return report('not_supported')
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    const outcome = await Promise.race([
      // DOM typings on older Chromium releases predate the string modes.
      track.applyConstraints({ echoCancellation: { exact: 'all' } } as unknown as MediaTrackConstraints)
        .then(() => 'accepted' as const, () => 'rejected' as const),
      new Promise<'timed_out'>(resolve => { timer = setTimeout(() => resolve('timed_out'), 1200) }),
    ])
    return report(ended() ? 'ended' : outcome)
  } catch { return report('rejected') }
  finally { if (timer !== undefined) clearTimeout(timer) }
}
