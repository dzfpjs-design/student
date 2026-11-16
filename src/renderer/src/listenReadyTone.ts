import type { SpeechArbiter } from './speechArbiter'
import type { ListenReadyAttempt } from './listenReadyCue'

/** Non-language readiness sound; it never enters Companion speech or grants capture. */
export function playListenReadyTone(arbiter: SpeechArbiter, attempt: ListenReadyAttempt, sessionId: string): Promise<boolean> {
  return new Promise(resolve => {
    let context: AudioContext | undefined, source: AudioBufferSourceNode | undefined
    let stopped = false, complete = false, closing: Promise<boolean> | undefined
    const cleanup = (natural: boolean): Promise<boolean> => {
      if (closing) return closing
      complete = true; stopped = !natural
      if (!natural) { try { source?.stop() } catch { /* An unstarted/ended owned source still disconnects. */ } }
      try { source?.disconnect() } catch { /* Independently close the owned context. */ }
      closing = Promise.resolve().then(async () => {
        if (!context) return false
        try {
          const owned=context,closed=owned.close(),remaining=Math.max(0,attempt.deadline-Date.now())
          let timer:ReturnType<typeof setTimeout>|undefined
          const result=await Promise.race([closed.then(()=>owned.state==='closed'&&natural),
            new Promise<boolean>(resolve=>{timer=setTimeout(()=>resolve(false),remaining)})])
          clearTimeout(timer);return result
        } catch { return false }
      })
      return closing
    }
    arbiter.submit({ scope: sessionId, key: attempt.key, priority: 'reminder', expiresAt: attempt.deadline,
      isCurrent: attempt.current, playbackTimeoutMs: Math.min(8000, attempt.deadline - Date.now()),
      play: (finish, unavailable) => {
        void (async () => {
          if (!attempt.current()) { unavailable(); return }
          context = new AudioContext()
          const buffer = context.createBuffer(1, Math.ceil(context.sampleRate * .12), context.sampleRate)
          const pcm = buffer.getChannelData(0), ramp = Math.max(1, Math.floor(context.sampleRate * .01))
          for (let i = 0; i < pcm.length; i++) pcm[i] = .08 * Math.sin(2 * Math.PI * 880 * i / context.sampleRate)
            * Math.min(1, i / ramp, (pcm.length - 1 - i) / ramp)
          source = context.createBufferSource(); source.buffer = buffer; source.connect(context.destination)
          await context.resume()
          if (complete || !attempt.current() || context.state !== 'running') { await cleanup(false); unavailable(); return }
          const actualContext = context, actualSource = source, expectedEnd = context.currentTime + buffer.duration
          source.onended = event => {
            if (complete || stopped) return
            if (event.isTrusted !== true || !attempt.current()
              || context !== actualContext || source !== actualSource || actualContext.state !== 'running' || actualContext.currentTime < expectedEnd) {
              void cleanup(false).then(unavailable); return
            }
            void cleanup(true).then(closed => { if (closed && attempt.current()) finish(); else unavailable() })
          }
          source.start()
        })().catch(() => { void cleanup(false).then(unavailable) })
      },
      settled: disposition => { void cleanup(disposition === 'finished').then(natural => resolve(disposition === 'finished' && natural)) },
    })
  })
}
