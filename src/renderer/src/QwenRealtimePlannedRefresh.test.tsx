import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { QwenRealtimeVoiceInput, QWEN_REALTIME_SESSION_REFRESH_MS, QWEN_38_REALTIME_SESSION_REFRESH_MS } from './QwenRealtimeVoiceInput'
import type { QwenRealtimeSessionCallbacks } from './qwenRealtimeVoice'
import { claimVoiceCapture, resetVoiceCaptureCoordinatorForTests } from './voiceCaptureCoordinator'

const mock = vi.hoisted(() => ({
  transports: [] as { callbacks: QwenRealtimeSessionCallbacks; close: ReturnType<typeof vi.fn>;
    state: { current: boolean; known: boolean; fresh: boolean; pending: boolean }; readPlannedRefreshState: ReturnType<typeof vi.fn> }[],
  subscribe: null as null | ((state: 'idle' | 'speaking' | 'preparing') => void), model: undefined as string | undefined, failRemaining: 0, waiting: [] as unknown[], retained: false,
  register: vi.fn(), release: vi.fn(), renew: vi.fn(), diagnostic: vi.fn(),
}))
vi.mock('./qwenRealtimeVoice', () => ({ QwenRealtimeVoiceSession: class {
  close = vi.fn(); interrupt = vi.fn(); setExternalPlaybackActive = vi.fn(); syncNativeAuthority = vi.fn()
  syncConversationActivation = vi.fn(); pending: unknown[] = []
  enterLocalWakeRecovery = () => mock.retained
  getConnectedModel = () => mock.model
  state = { current: true, known: true, fresh: true, pending: false }
  readPlannedRefreshState = vi.fn(async () => this.state)
  takePendingInputsForReconnect = () => [...this.pending.splice(0), ...mock.waiting.splice(0)]; resumePendingInputs = vi.fn((items: unknown[]) => this.pending.push(...items))
  constructor(public options: { callbacks: QwenRealtimeSessionCallbacks }) { mock.transports.push(this) }
  get callbacks() { return this.options.callbacks }
  async connect() { if (mock.failRemaining > 0) { mock.failRemaining--; throw Error('public transport unavailable') }
    await this.callbacks.onMicrophoneOpen?.({} as MediaStreamTrack); this.callbacks.onStatus({
    runtime: 'qwen_omni_realtime', phase: 'listening', speechActive: false, pendingUtterances: 0, recoverable: true }) }
} }))
vi.mock('./mediaDevices', () => ({ mediaDeviceRegistration: vi.fn().mockResolvedValue({ systemIdHash: 'public', capabilities: {} }) }))
vi.mock('./api', () => ({ localApi: { registerDevice: mock.register, releaseDeviceLease: mock.release, renewDeviceLease: mock.renew } }))
vi.mock('./companionVoice', async original => ({ ...await original<typeof import('./companionVoice')>(),
  getCompanionVoiceState: () => 'idle', getCompanionPlaybackLocalOnly: () => true,
  subscribeCompanionVoice: (fn: NonNullable<typeof mock.subscribe>) => { mock.subscribe = fn; return () => { if (mock.subscribe === fn) mock.subscribe = null } },
  stopCompanionSpeech: vi.fn(), speakCompanion: vi.fn() }))
beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(100_000); mock.transports.length = 0; mock.subscribe = null; mock.model = undefined; mock.failRemaining = 0; mock.waiting = []; mock.retained = false;
  mock.register.mockReset().mockResolvedValue({ lease_id: 'public-lease' }); mock.release.mockReset().mockResolvedValue({});
  mock.renew.mockReset().mockResolvedValue({}); mock.diagnostic.mockReset(); resetVoiceCaptureCoordinatorForTests();
  Object.defineProperty(window, 'desktop', { configurable: true, value: { recordRendererDiagnostic: mock.diagnostic } }) })
afterEach(() => { cleanup(); resetVoiceCaptureCoordinatorForTests(); vi.useRealTimers() })
const advance = async (ms: number) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }
async function mounted() { const view = render(<QwenRealtimeVoiceInput active audioAllowed nativeRuntime={{} as never} initialConversationActive
  sessionId="public-session" childId="public-child" childName="公开合成" onTurn={vi.fn()} onStatus={vi.fn()} />);
  await advance(0); expect(mock.transports).toHaveLength(1); return view }

it('protects a real companion reply end plus 2s at the scheduled rotation boundary', async () => {
  await mounted(); await advance(QWEN_REALTIME_SESSION_REFRESH_MS - 2_000);
  await act(async () => { mock.subscribe?.('speaking'); mock.subscribe?.('idle') });
  await advance(2_000); expect(mock.transports).toHaveLength(1); expect(mock.transports[0].close).not.toHaveBeenCalled();
  await advance(10_000); expect(mock.transports).toHaveLength(2);
  expect(mock.diagnostic.mock.calls.filter(([x]) => x.event === 'voice.realtime.session_rotated')).toHaveLength(1)
})
it.each(['preparing', 'pending_status', 'unknown', 'stale', 'unconfirmed_audio', 'retired_source'] as const)('defers a planned refresh while %s', async condition => {
  await mounted(); await advance(QWEN_REALTIME_SESSION_REFRESH_MS - 100);
  const t = mock.transports[0];
  await act(async () => { if (condition === 'preparing') mock.subscribe?.('preparing');
    else if (condition === 'pending_status') t.callbacks.onStatus({ runtime: 'qwen_omni_realtime', phase: 'listening', speechActive: false, pendingUtterances: 1, recoverable: true });
    else t.state = { ...t.state, ...(condition === 'unknown' ? { known: false } : condition === 'stale' ? { fresh: false }
      : condition === 'unconfirmed_audio' ? { pending: true } : { current: false }) } });
  await advance(10_100); expect(mock.transports).toHaveLength(1); expect(t.close).not.toHaveBeenCalled()
})
it('refreshes a genuinely quiet current transport once', async () => {
  await mounted(); await advance(QWEN_REALTIME_SESSION_REFRESH_MS); expect(mock.transports).toHaveLength(2);
  expect(mock.transports[0].readPlannedRefreshState).toHaveBeenCalledTimes(1); expect(mock.transports[0].close).toHaveBeenCalledTimes(1)
})
it.each(['preparing', 'child_activity', 'privacy', 'unmount', 'new_session', 'reject'] as const)('rechecks current ownership and activity after the bounded stats await: %s', async condition => {
  const view = await mounted(); const old = mock.transports[0];
  let resolve!: (value: typeof old.state) => void, reject!: (reason: Error) => void;
  old.readPlannedRefreshState.mockImplementationOnce(() => new Promise((yes, no) => { resolve = yes; reject = no }));
  await advance(QWEN_REALTIME_SESSION_REFRESH_MS); expect(mock.transports).toHaveLength(1);
  await act(async () => {
    if (condition === 'preparing') mock.subscribe?.('preparing');
    if (condition === 'child_activity') old.callbacks.onStatus({ runtime: 'qwen_omni_realtime', phase: 'listening', speechActive: true, pendingUtterances: 0, recoverable: true });
    if (condition === 'privacy') window.dispatchEvent(new CustomEvent('hsc-privacy-pause'));
    if (condition === 'unmount') view.unmount();
    if (condition === 'new_session') view.rerender(<QwenRealtimeVoiceInput active audioAllowed nativeRuntime={{} as never} initialConversationActive sessionId="new-public-session" childId="public-child" childName="公开合成" onTurn={vi.fn()} onStatus={vi.fn()} />);
  });
  await act(async () => {
    if (condition === 'reject') reject(Error('private stats transport')); 
    else resolve({ current: true, known: true, fresh: true, pending: false });
    await Promise.resolve();
  });
  await advance(100);
  expect(mock.transports).toHaveLength(condition === 'new_session' ? 2 : 1);
  expect(mock.diagnostic.mock.calls.some(([x]) => x.event === 'voice.realtime.session_rotated')).toBe(false)
})

it('keeps a confirmed Qwen 3.8 transport through the old five-minute boundary, then rotates only when idle', async () => {
  mock.model = 'qwen3.8-omni-flash-realtime'; await mounted()
  await advance(QWEN_REALTIME_SESSION_REFRESH_MS + 5_000)
  expect(mock.transports).toHaveLength(1); expect(mock.transports[0].close).not.toHaveBeenCalled()
  await advance(QWEN_38_REALTIME_SESSION_REFRESH_MS - QWEN_REALTIME_SESSION_REFRESH_MS - 5_000)
  expect(mock.transports).toHaveLength(2); expect(mock.transports[0].close).toHaveBeenCalledTimes(1)
})
it('still defers the longer Qwen 3.8 rotation while audio ownership is unconfirmed', async () => {
  mock.model = 'qwen3.8-omni-flash-realtime'; await mounted()
  mock.transports[0].state = { current: true, known: true, fresh: true, pending: true }
  await advance(QWEN_38_REALTIME_SESSION_REFRESH_MS + 5_000)
  expect(mock.transports).toHaveLength(1); expect(mock.transports[0].close).not.toHaveBeenCalled()
})

async function exhaust() {
  mock.failRemaining = 4; const view = await mounted()
  await advance(1_000 + 3_000 + 8_000)
  expect(mock.transports).toHaveLength(4)
  return view
}
it('automatically probes after the fast retry budget and never resumes an original input', async () => {
  await exhaust(); await advance(29_999); expect(mock.transports).toHaveLength(4)
  await advance(1); expect(mock.transports).toHaveLength(5)
  expect((mock.transports[4] as any).resumePendingInputs).not.toHaveBeenCalled()
  expect((mock.transports[4] as any).options.nativeActivation.canRecoverPersisted()).toBe(false)
})
it('backs off failed background probes and recovers without replaying previous inputs', async () => {
  await exhaust(); mock.failRemaining = 2
  await advance(30_000); expect(mock.transports).toHaveLength(5)
  await advance(59_999); expect(mock.transports).toHaveLength(5)
  await advance(1); expect(mock.transports).toHaveLength(6)
  await advance(119_999); expect(mock.transports).toHaveLength(6)
  await advance(1); expect(mock.transports).toHaveLength(7)
  expect((mock.transports[6] as any).resumePendingInputs).not.toHaveBeenCalled()
  expect((mock.transports[6] as any).options.nativeActivation.canRecoverPersisted()).toBe(false)
})
it('cancels a subsequent slow probe when privacy is paused after an unsuccessful probe', async () => {
  await exhaust(); mock.failRemaining = 1
  await advance(30_000); expect(mock.transports).toHaveLength(5)
  await act(async () => { window.dispatchEvent(new CustomEvent('hsc-privacy-pause')) })
  await advance(180_000); expect(mock.transports).toHaveLength(5)
})
it.each(['privacy','unmount','new_session','permission'] as const)('does not revive a retired recovery after %s', async condition => {
  const view = await exhaust()
  await act(async () => {
    if (condition === 'privacy') window.dispatchEvent(new CustomEvent('hsc-privacy-pause'))
    if (condition === 'unmount') view.unmount()
    if (condition === 'new_session') view.rerender(<QwenRealtimeVoiceInput active audioAllowed nativeRuntime={{} as never} initialConversationActive sessionId="new-public-session" childId="new-public-child" childName="公开合成" onTurn={vi.fn()} onStatus={vi.fn()} />)
    if (condition === 'permission') view.rerender(<QwenRealtimeVoiceInput active audioAllowed={false} nativeRuntime={{} as never} initialConversationActive sessionId="public-session" childId="public-child" childName="公开合成" onTurn={vi.fn()} onStatus={vi.fn()} />)
  })
  await advance(45_000)
  expect(mock.transports).toHaveLength(condition === 'new_session' ? 5 : 4)
})
it('never steals another recorder while waiting for recovery', async () => {
  await exhaust(); expect(claimVoiceCapture('different-owner')).toBe(true)
  await advance(180_000); expect(mock.transports).toHaveLength(4)
})
it('holds the original unfinished household entry instead of resubmitting it in the background', async () => {
  mock.failRemaining=4
  render(<QwenRealtimeVoiceInput active audioAllowed nativeRuntime={{} as never} householdEntry={{} as never} initialConversationActive sessionId="public-session" childId="public-child" childName="公开合成" onTurn={vi.fn()} onStatus={vi.fn()} />)
  await advance(12_000); expect(mock.transports).toHaveLength(4)
  await advance(180_000); expect(mock.transports).toHaveLength(4)
})

it('never schedules background reconnect after a local safety stop', async () => {
  await mounted(); await act(async()=>mock.transports[0].callbacks.onLocalSafety?.({} as never, false))
  await advance(180_000); expect(mock.transports).toHaveLength(1)
})
it('keeps unconfirmed original inputs rather than replaying them on background recovery', async () => {
  mock.failRemaining=4; await mounted(); await advance(4_000)
  mock.waiting=[{utterance:'public pending request'}]
  await advance(8_000); expect(mock.transports).toHaveLength(4)
  await advance(180_000); expect(mock.transports).toHaveLength(4)
})

it('manual wake cancels the slow timer and a fresh failure can retry without duplicate connections', async () => {
  mock.retained=true; await exhaust(); mock.failRemaining=1
  await act(async()=>{(mock.transports[3] as any).options.nativeActivation.onRecoveryWake(); await Promise.resolve()})
  expect(mock.transports).toHaveLength(5)
  await advance(1_000); expect(mock.transports).toHaveLength(6)
  await advance(30_000); expect(mock.transports).toHaveLength(6)
})

it('defers a background connection while the local companion is speaking', async () => {
  await exhaust(); await act(async()=>mock.subscribe?.('speaking'))
  await advance(30_000); expect(mock.transports).toHaveLength(4)
  await act(async()=>mock.subscribe?.('idle')); await advance(60_000)
  expect(mock.transports).toHaveLength(5)
})

it('manual wake does not replay an unconsumed household entry after exhausted retries', async () => {
  mock.retained=true; mock.failRemaining=4
  render(<QwenRealtimeVoiceInput active audioAllowed nativeRuntime={{} as never} householdEntry={{} as never} initialConversationActive sessionId="public-session" childId="public-child" childName="公开合成" onTurn={vi.fn()} onStatus={vi.fn()} />)
  await advance(12_000); expect(mock.transports).toHaveLength(4)
  await act(async()=>{(mock.transports[3] as any).options.nativeActivation.onRecoveryWake(); await Promise.resolve()})
  await advance(35_000); expect(mock.transports).toHaveLength(4)
})
it('manual wake does not replay unconfirmed pending input after exhausted retries', async () => {
  mock.retained=true;mock.failRemaining=4;await mounted();await advance(4_000)
  mock.waiting=[{utterance:'public pending request'}];await advance(8_000)
  await act(async()=>{(mock.transports[3] as any).options.nativeActivation.onRecoveryWake();await Promise.resolve()})
  await advance(35_000);expect(mock.transports).toHaveLength(4)
})
it('manual wake and background probe cannot race an unconfirmed original-input handoff', async () => {
  mock.retained=true;mock.failRemaining=4
  const handoff=vi.fn(()=>new Promise<boolean>(()=>undefined))
  render(<QwenRealtimeVoiceInput active audioAllowed nativeRuntime={{} as never} initialConversationActive
    sessionId="public-session" childId="public-child" childName="公开合成" onTurn={vi.fn()} onStatus={vi.fn()} onPendingInputHandoff={handoff} />)
  await advance(4_000);mock.waiting=[{utterance:'public pending request'}];await advance(8_000)
  expect(handoff).toHaveBeenCalledTimes(1)
  await act(async()=>{(mock.transports[3] as any).options.nativeActivation.onRecoveryWake();await Promise.resolve()})
  await advance(35_000);expect(mock.transports).toHaveLength(4)
})
