import { afterEach, describe, expect, it, vi } from 'vitest'
import { playPublicReply } from './publicReplyPlayback'
const queued = vi.hoisted(() => ({ play: vi.fn(), cancel: vi.fn() }))
vi.mock('./LocalInputs', () => ({ queueManagedReply: (...args: unknown[]) => queued.play(...args), cancelLocalSpeech: (...args: unknown[]) => queued.cancel(...args) }))
const source = { sessionId: 'synthetic_session', childId: 'synthetic_child', agentTurnId: 'synthetic_turn' }
const audio = { leaseId: 'synthetic_lease', responseId: 'synthetic_response', voice: 'Tina', model: 'qwen3.8-omni-flash-realtime',
  pcmBase64: btoa(String.fromCharCode(0,0,0,0)), sampleRate: 24000 as const, sampleCount: 2, frameCount: 1,
  generationCompleted: true as const, renderedCompleted: false as const }
function desktop(status: 'eligible' | 'unavailable' | 'unconfirmed' = 'eligible') {
  const receipts: string[] = []; let retire: ((id: string) => void) | undefined
  const bridge = { openPublicReplySpeech: vi.fn(async () => status === 'eligible' ? { status, leaseId: audio.leaseId } : { status }),
    generatePublicReplySpeech: vi.fn(async () => audio), settlePublicReplySpeech: vi.fn(async (_: string, event: string) => { receipts.push(event) }),
    cancelPublicReplySpeech: vi.fn(async () => undefined), onPublicReplySpeechRetired: vi.fn((callback: (id: string) => void) => { retire=callback; return vi.fn() }) }
  Object.defineProperty(window, 'desktop', { configurable: true, value: bridge })
  return { ...bridge, receipts, retire: () => retire?.(audio.leaseId) }
}
afterEach(() => { vi.clearAllMocks(); Object.defineProperty(window,'desktop',{configurable:true,value:undefined}) })
describe('public reply in the shared player', () => {
  it('waits for durable rendered receipt before acknowledging completion', async () => {
    const bridge = desktop(), ended=vi.fn(), results=vi.fn()
    let acknowledge!: () => void
    bridge.settlePublicReplySpeech.mockImplementation(async (_: string, event: string) => {
      bridge.receipts.push(event)
      if (event==='rendered') await new Promise<void>(resolve=>{acknowledge=resolve})
    })
    queued.play.mockImplementation(async (provider, _: string, __: string, started, options) => {
      const chunks=[]; for await(const chunk of provider.stream({signal:new AbortController().signal})) chunks.push(chunk)
      expect(chunks[0].pcm16.byteLength).toBe(4); started(); options.onPlaybackEnd(); return 'managed'
    })
    expect(await playPublicReply(source,'本地显示文字',{onPlaybackEnd:ended,isCurrent:()=>true},results)).toBe('handled')
    await vi.waitFor(()=>expect(bridge.receipts).toEqual(['started','rendered']))
    expect(ended).not.toHaveBeenCalled(); expect(results).not.toHaveBeenCalledWith('managed')
    acknowledge(); await vi.waitFor(()=>expect(ended).toHaveBeenCalledOnce())
    expect(results).toHaveBeenCalledWith('managed')
    expect(bridge.openPublicReplySpeech).toHaveBeenCalledWith(source)
    expect(bridge.generatePublicReplySpeech).toHaveBeenCalledWith(audio.leaseId)
  })
  it('cannot fall back or repeat after an uncertain reservation', async () => {
    const bridge=desktop('unconfirmed'), result=vi.fn()
    expect(await playPublicReply(source,'私密本地内容',{},result)).toBe('handled')
    expect(result).toHaveBeenCalledWith('unconfirmed'); expect(queued.play).not.toHaveBeenCalled()
    expect(bridge.generatePublicReplySpeech).not.toHaveBeenCalled()
  })
  it('keeps a definitively ineligible local reply on its existing local boundary', async () => {
    desktop('unavailable')
    expect(await playPublicReply(source,'私密本地内容',{},vi.fn())).toBe('unavailable')
    expect(queued.play).not.toHaveBeenCalled()
  })
  it('cancels only the original speech key on Runtime revocation', async () => {
    const bridge=desktop(), ended=vi.fn()
    queued.play.mockImplementation(async (_: unknown, __: string, ___: string, ____: unknown, options) => {
      bridge.retire(); expect(options.isCurrent()).toBe(false); options.onNotDelivered('cancelled'); return 'text_only'
    })
    await playPublicReply(source,'公开回复',{onPlaybackEnd:ended},vi.fn())
    expect(queued.cancel).toHaveBeenCalledWith('runtime-public-synthetic_turn')
    expect(ended).not.toHaveBeenCalled(); expect(bridge.receipts).not.toContain('rendered')
  })
  it('a lost started acknowledgment never becomes rendered or an automatic retry', async () => {
    const bridge=desktop(), ended=vi.fn(), result=vi.fn()
    bridge.settlePublicReplySpeech.mockRejectedValue(Error('lost acknowledgment'))
    queued.play.mockImplementation(async (_: unknown, __: string, ___: string, started, options) => { started(); options.onPlaybackEnd(); return 'managed' })
    await playPublicReply(source,'公开回复',{onPlaybackEnd:ended},result)
    await vi.waitFor(()=>expect(result).toHaveBeenCalledWith('unconfirmed'))
    expect(ended).not.toHaveBeenCalled(); expect(bridge.openPublicReplySpeech).toHaveBeenCalledOnce()
    expect(bridge.settlePublicReplySpeech).not.toHaveBeenCalledWith(audio.leaseId,'rendered')
  })
  it('persists a known playback interruption before retiring its main lease', async () => {
    const bridge=desktop(), ended=vi.fn(), result=vi.fn()
    queued.play.mockImplementation(async (provider, _: string, __: string, started, options) => {
      const abort=new AbortController(), stream=provider.stream({text:'unused',signal:abort.signal})
      await stream.next();started();abort.abort();options.onNotDelivered('cancelled');await stream.return()
      return 'text_only'
    })
    await playPublicReply(source,'公开回复',{onPlaybackEnd:ended},result)
    await vi.waitFor(()=>expect(bridge.receipts).toEqual(['started','interrupted']))
    expect(bridge.cancelPublicReplySpeech).not.toHaveBeenCalled();expect(ended).not.toHaveBeenCalled()
    await vi.waitFor(()=>expect(result).toHaveBeenCalledWith('interrupted'))
    expect(result).not.toHaveBeenCalledWith('managed')
  })
})
