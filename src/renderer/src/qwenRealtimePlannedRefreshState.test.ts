import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { QwenRealtimeVoiceSession } from './qwenRealtimeVoice'

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())
async function transport() {
  let energy = 0, duration = 0, foreign = false;
  const track = { id: 'owned-track', enabled: false, muted: false, readyState: 'live', getSettings: () => ({}), stop: vi.fn() };
  const root = { ...track, enabled: true }; const error = vi.fn();
  const peer = { connectionState: 'connected', close: vi.fn(), getStats: vi.fn(async () => { duration += 1; return new Map([['owned', {
    id: 'owned-source', type: 'media-source', kind: 'audio', trackIdentifier: foreign ? 'foreign-track' : track.id,
    totalAudioEnergy: energy, totalSamplesDuration: duration }]]) }) };
  const session = Object.assign(new QwenRealtimeVoiceSession({ childName: 'public test', callbacks: { onStatus: vi.fn(), onError: error, onCommittedTurn: vi.fn().mockResolvedValue(undefined),
    captureInputContext: () => ({ sessionId: 'public-session', reference: { child_id: 'public-child' }, carryoverConfirmationId: null }) } }),
    { peer, sender: { track, replaceTrack: vi.fn().mockResolvedValue(undefined) }, audioTrack: track, localSourceTrack: root, stream: { getTracks: () => [root, track] } }) as unknown as
    Pick<QwenRealtimeVoiceSession, 'close' | 'readPlannedRefreshState'> & {
      receive(raw: string): void; audioHealthEpoch: number;
      audioActivityHealth: { reset(): void; snapshot(at: number): { known: boolean; fresh: boolean; pending: boolean } };
      pendingToolCalls: unknown[]
    };
  session.receive(JSON.stringify({ type: 'session.updated' })); await Promise.resolve();
  return { session, track, root, peer, error, duration: () => duration, energy: (x: number) => { energy = x }, foreign: () => { foreign = true } };
}
const advance = async (ms: number) => { await vi.advanceTimersByTimeAsync(ms) };
it('reads current fresh known quiet stats without words or PCM', async () => {
  const t = await transport(); await advance(2_000);
  expect(await t.session.readPlannedRefreshState()).toEqual({ current: true, known: true, fresh: true, pending: false });
  t.session.close()
})
it('first measurement is unknown and local activity remains pending until genuine completion', async () => {
  const t = await transport(); expect((await t.session.readPlannedRefreshState()).known).toBe(false);
  await advance(1_000); t.energy(0.1); await advance(1_000);
  expect((await t.session.readPlannedRefreshState()).pending).toBe(true);
  t.session.receive(JSON.stringify({ type: 'input_audio_buffer.speech_started', item_id: 'public-input' }));
  t.session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'public-input', transcript: '公开合成原话' }));
  await advance(2_000); expect((await t.session.readPlannedRefreshState()).pending).toBe(false); t.session.close()
})
it.each(['foreign', 'released', 'root_ended', 'root_disabled', 'root_muted', 'queue', 'accepted_onset'] as const)('does not rotate through %s', async condition => {
  const t = await transport(); await advance(2_000);
  if (condition === 'foreign') { t.foreign(); await advance(500) }
  if (condition === 'released') t.track.enabled = false;
  if (condition === 'root_ended') t.root.readyState = 'ended';
  if (condition === 'root_disabled') t.root.enabled = false;
  if (condition === 'root_muted') t.root.muted = true;
  if (condition === 'queue') t.session.pendingToolCalls.push({ utterance: 'private test never exported' });
  if (condition === 'accepted_onset') t.session.receive(JSON.stringify({ type: 'input_audio_buffer.speech_started', item_id: 'public-pending' }));
  const state = await t.session.readPlannedRefreshState();
  expect(state.pending || !state.current || !state.known).toBe(true); expect(Object.keys(state).sort()).toEqual(['current','fresh','known','pending']);
  t.session.close()
})
it('joins the existing bounded health poll without issuing another stats request', async () => {
  const t = await transport(); await advance(2_000);
  let resolve!: (value: Awaited<ReturnType<typeof t.peer.getStats>>) => void;
  t.peer.getStats.mockImplementationOnce(() => new Promise(yes => { resolve = yes }) as ReturnType<typeof t.peer.getStats>);
  await advance(1_000); const requests = t.peer.getStats.mock.calls.length;
  const reading = t.session.readPlannedRefreshState(); await Promise.resolve();
  expect(t.peer.getStats).toHaveBeenCalledTimes(requests);
  resolve(new Map([['owned', { id: 'owned-source', type: 'media-source', kind: 'audio', trackIdentifier: t.track.id,
    // Continue the actual fixture sequence after any number of normal polls.
    totalAudioEnergy: 0, totalSamplesDuration: t.duration() + 1 }]]));
  expect(await reading).toEqual({ current: true, known: true, fresh: true, pending: false });
  expect(t.peer.getStats).toHaveBeenCalledTimes(requests); t.session.close()
})
it('marks a quiet measurement stale after two seconds without renewing it', async () => {
  const t = await transport(); await advance(2_000);
  expect(t.session.audioActivityHealth.snapshot(performance.now() + 2_001)).toEqual({ known: true, fresh: false, pending: false });
  t.session.close()
})
it.each(['deadline', 'epoch', 'close'] as const)('a delayed current-peer read cannot revive measurement after %s', async condition => {
  const t = await transport(); await advance(2_000);
  let resolve!: (value: Awaited<ReturnType<typeof t.peer.getStats>>) => void;
  t.peer.getStats.mockImplementationOnce(() => new Promise(yes => { resolve = yes }) as ReturnType<typeof t.peer.getStats>);
  // Maintenance reuses fresh cache; enter the original normal poll to test its in-flight await.
  await advance(500);
  const reading = t.session.readPlannedRefreshState(); await Promise.resolve();
  if (condition === 'epoch') { t.session.audioHealthEpoch++; t.session.audioActivityHealth.reset() }
  if (condition === 'close') t.session.close();
  await advance(2_000); const state = await reading;
  expect(!state.current || !state.known || !state.fresh).toBe(true);
  resolve(new Map([['owned', { id: 'owned-source', type: 'media-source', kind: 'audio', trackIdentifier: t.track.id,
    totalAudioEnergy: 10, totalSamplesDuration: 99 }]]));
  await Promise.resolve(); await advance(1_000); expect(t.error).not.toHaveBeenCalled(); t.session.close()
})
it('preserves the existing 30s unconfirmed-audio failure instead of refreshing its timer', async () => {
  const t = await transport(); await advance(1_000); t.energy(0.1); await advance(1_000);
  for (let i = 0; i < 29; i++) { expect((await t.session.readPlannedRefreshState()).pending).toBe(true); await advance(1_000) }
  await advance(1_000); expect(t.error).toHaveBeenCalledExactlyOnceWith(expect.stringContaining('30秒')); t.session.close()
})
