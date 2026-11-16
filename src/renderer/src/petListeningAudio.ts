import type { PetMedia } from './petMedia'

/** Browser WAV decoder is used at the original rate; no guessed RIFF offsets. */
export async function decodePetListening(bytes: ArrayBuffer, media: PetMedia): Promise<{ pcm: ArrayBuffer; sampleRate: number }> {
  const audio = media.audio, rate = audio.sample_rate, frames = audio.frames
  if (media.kind !== 'listening' || !media.source_reviewed || audio.status !== 'usable_signal'
    || audio.channels !== 1 || audio.sample_width !== 2 || !rate || ![16000, 24000, 48000].includes(rate)
    || !frames || !Number.isSafeInteger(frames) || frames < rate / 2 || frames > rate * 600
    || bytes.byteLength > 24 * 1024 * 1024 || bytes.byteLength < 44 || !/^[a-f0-9]{64}$/.test(audio.sha256 ?? '')) {
    throw new Error('PET_LISTENING_AUDIO_NOT_VERIFIED')
  }
  const digest = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))).map(x => x.toString(16).padStart(2, '0')).join('')
  if (digest !== audio.sha256) throw new Error('PET_LISTENING_AUDIO_CHANGED')
  const context = new OfflineAudioContext(1, frames, rate)
  const decoded = await context.decodeAudioData(bytes.slice(0))
  if (decoded.numberOfChannels !== 1 || decoded.sampleRate !== rate || decoded.length !== frames) throw new Error('PET_LISTENING_FORMAT_CHANGED')
  const samples = decoded.getChannelData(0), pcm = new Int16Array(frames)
  for (let index = 0; index < samples.length; index++) {
    if (!Number.isFinite(samples[index])) throw new Error('PET_LISTENING_INVALID_SAMPLE')
    pcm[index] = Math.min(32767, Math.max(-32768, Math.round(samples[index] * 32768)))
  }
  return { pcm: pcm.buffer, sampleRate: rate }
}
