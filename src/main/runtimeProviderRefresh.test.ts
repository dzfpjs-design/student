// @vitest-environment node
import { expect, it, vi } from 'vitest'
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { RuntimeProviderRefresh, cloudProviderSettingsFingerprint } from './runtime-provider-refresh'

function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(r => { resolve = r }); return { promise, resolve } }
function fixture(coordinator = new RuntimeProviderRefresh()) {
  const dependencies = {
    assertCurrent: vi.fn(), fingerprint: vi.fn(async () => 'synthetic-opaque'),
    loadSnapshot: vi.fn(async () => ({ modelConfig: null, asrConfig: null, voiceConfig: null })),
    admit: vi.fn(() => ({ syntheticAdmission: true })),
    apply: vi.fn(async () => ({ syntheticApplied: true })), applied: vi.fn(() => true), result: vi.fn(() => 'ready'),
  }
  return { coordinator, dependencies }
}
it.each(['save', 'clear', 'realtime-save', 'realtime-clear', 'voice-save', 'voice-clear', 'reference-save'])('rejects an in-flight load when a %s request arrives, before committing stale cache', async () => {
  const f = fixture(); const entered = deferred<void>(); const loaded = deferred<Awaited<ReturnType<typeof f.dependencies.loadSnapshot>>>()
  f.dependencies.loadSnapshot.mockImplementation(async () => { entered.resolve(); return loaded.promise })
  const pending = f.coordinator.reload(f.dependencies); const rejected = expect(pending).rejects.toThrow('CLOUD_PROVIDER_REFRESH_CHANGED')
  await entered.promise
  const mutation = vi.fn(async () => 'saved'); const saved = f.coordinator.mutate(mutation)
  loaded.resolve({ modelConfig: null, asrConfig: null, voiceConfig: null })
  await rejected; await saved
  expect(f.dependencies.apply).not.toHaveBeenCalled(); expect(mutation).toHaveBeenCalledOnce()
})
it('rejects opaque file changes across a load without applying or rewriting', async () => {
  const f = fixture(); f.dependencies.fingerprint.mockResolvedValueOnce('before').mockResolvedValueOnce('after')
  await expect(f.coordinator.reload(f.dependencies)).rejects.toThrow('CLOUD_PROVIDER_SETTINGS_CHANGED')
  expect(f.dependencies.apply).not.toHaveBeenCalled()
})
it('does not rollback cache or report disabled when a mutation arrives during actual restart', async () => {
  const f = fixture(); const entered = deferred<void>(); const applied = deferred<{ syntheticApplied: boolean }>()
  f.dependencies.apply.mockImplementation(async () => { entered.resolve(); return applied.promise })
  const pending = f.coordinator.reload(f.dependencies); const rejected = expect(pending).rejects.toThrow('CLOUD_PROVIDER_REFRESH_CHANGED')
  await entered.promise
  const mutate = f.coordinator.mutate(async () => 'new-config')
  applied.resolve({ syntheticApplied: true }); await rejected; await mutate
  expect(f.dependencies.apply).toHaveBeenCalledOnce(); expect(f.dependencies.result).not.toHaveBeenCalled()
})
it('rechecks exact applied identity after the final asynchronous fingerprint', async () => {
  const f = fixture(); f.dependencies.fingerprint.mockImplementation(async () => {
    if (f.dependencies.apply.mock.calls.length) f.dependencies.applied.mockReturnValue(false)
    return 'synthetic-opaque'
  })
  await expect(f.coordinator.reload(f.dependencies)).rejects.toThrow('CLOUD_PROVIDER_REFRESH_NOT_APPLIED')
  expect(f.dependencies.result).not.toHaveBeenCalled()
})
it('rechecks sender/scope after the final asynchronous fingerprint', async () => {
  const f = fixture(); f.dependencies.fingerprint.mockImplementation(async () => {
    if (f.dependencies.apply.mock.calls.length) f.dependencies.assertCurrent.mockImplementation(() => { throw Error('changed-window') })
    return 'synthetic-opaque'
  })
  await expect(f.coordinator.reload(f.dependencies)).rejects.toThrow('changed-window')
  expect(f.dependencies.result).not.toHaveBeenCalled()
})
it('busy admission fails before protected settings are read', async () => {
  const f = fixture(); f.dependencies.admit.mockImplementation(() => { throw Error('busy') })
  await expect(f.coordinator.reload(f.dependencies)).rejects.toThrow('busy')
  expect(f.dependencies.fingerprint).not.toHaveBeenCalled(); expect(f.dependencies.loadSnapshot).not.toHaveBeenCalled()
})
it('failure does not poison the queue or turn failed reload into applied', async () => {
  const f = fixture(); await expect(f.coordinator.mutate(async () => { throw Error('save-failed') })).rejects.toThrow('save-failed')
  f.dependencies.applied.mockReturnValue(false)
  await expect(f.coordinator.reload(f.dependencies)).rejects.toThrow('CLOUD_PROVIDER_REFRESH_NOT_APPLIED')
  f.dependencies.applied.mockReturnValue(true)
  expect(await f.coordinator.reload(f.dependencies)).toBe('ready')
})
it('fingerprints only the five opaque cloud records, including durable disable and explicit absence', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'cloud-fingerprint-synthetic-'))
  try {
    const absent = await cloudProviderSettingsFingerprint(directory)
    await writeFile(join(directory, 'pet-audio-settings.json'), 'synthetic-unrelated-pet')
    expect(await cloudProviderSettingsFingerprint(directory)).toBe(absent)
    await writeFile(join(directory, 'voice-disabled.json'), '{"disabled":true}')
    const disabled = await cloudProviderSettingsFingerprint(directory); expect(disabled).not.toBe(absent)
    await writeFile(join(directory, 'model-settings.json'), '{"encryptedApiKey":"synthetic-ciphertext"}')
    expect(await cloudProviderSettingsFingerprint(directory)).not.toBe(disabled)
    await mkdir(join(directory, 'asr-settings.json'))
    await expect(cloudProviderSettingsFingerprint(directory)).rejects.toThrow('CLOUD_PROVIDER_SETTINGS_UNREADABLE')
  } finally { await rm(directory, { recursive: true, force: true }) }
})
