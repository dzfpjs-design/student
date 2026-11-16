import { realpathSync, mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { PetAudioSettingsCommand } from '../../shared/petAudioSettings'
const bridge = vi.hoisted(() => ({ api: null as unknown, invoke: vi.fn() }))
vi.mock('electron', () => ({ app: { getPath: () => '' }, contextBridge: { exposeInMainWorld: (_: string, api: unknown) => { bridge.api = api } },
  ipcRenderer: { invoke: (...args: unknown[]) => bridge.invoke(...args) },
  safeStorage: { isEncryptionAvailable: () => true, encryptString: (raw: string) => Buffer.from(Buffer.from(raw).toString('base64')),
    decryptString: (raw: Buffer) => Buffer.from(raw.toString(), 'base64').toString() }
}))
import '../../preload/index'
import { PetAudioSettingsStore, createPetAudioSettingsHandler } from '../../main/pet-audio-settings'
import { localApi, requestPetAudioSettings, resetRuntimeCache } from './api'
import { ParentPetAudioSettings } from './ParentPetAudioSettings'
import { PET_AUDIO_PURPOSE } from '../../shared/petAudioSettings'
import { parentPetPendingPrefix } from './petMaterialSupplyTypes'
import { PET_AUDIO_TARIFF } from '../../shared/petAudioTariffs'
let directory: string, store: PetAudioSettingsStore, session: string, parent: string, restart: boolean, loss: boolean, writes: number
const marker = parentPetPendingPrefix('primary') + 'original-audio-settings'
beforeEach(async () => {
  directory = mkdtempSync(join(realpathSync(tmpdir()), 'hsc-pet-recovery-test-')); store = new PetAudioSettingsStore(directory)
  session = 'session-before'; parent = 'parent-before'; restart = false; loss = false; writes = 0
  localStorage.clear(); resetRuntimeCache(); window.desktop = bridge.api as NonNullable<typeof window.desktop>
  const handler = createPetAudioSettingsHandler(store, async (_sender, child, token) => {
    if (!['primary', 'junior'].includes(child) || token !== parent) throw Error('Parent PIN expired')
  }, { apply: async () => { if (restart) { session = 'session-after'; parent = 'parent-after' } }, applied: () => true })
  bridge.invoke.mockReset().mockImplementation(async (channel: string, child: string, token: string, command: PetAudioSettingsCommand) => {
    if (channel === 'runtime:get-config') return { status: 'ready', localApiUrl: 'http://127.0.0.1:8765', sessionToken: session }
    if (channel !== 'pet-audio:settings') throw Error('Unexpected IPC')
    const result = await handler({ sender: { id: 7 } }, child, token, command)
    if (command.kind !== 'read') { writes++; if (loss) throw Error('Lost response after commit/restart') }
    return result
  })
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit) => {
    if (!url.endsWith('/v1/parent/unlock')) throw Error('No external requests allowed')
    if (new Headers(init.headers).get('X-HSC-Session') !== session) return new Response('{}', { status: 401 })
    return new Response(JSON.stringify({ parent_token: parent }), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }))
  await localApi.parentUnlock('synthetic-pin', false)
})
afterEach(() => { cleanup(); resetRuntimeCache(); vi.restoreAllMocks(); vi.unstubAllGlobals(); localStorage.clear(); rmSync(directory, { recursive: true, force: true }) })
async function open(): Promise<void> {
  render(<ParentPetAudioSettings childId="primary" />)
  await waitFor(() => expect(screen.getByRole('button', { name: '保存原音配置' })).toBeEnabled())
  fireEvent.change(screen.getByLabelText('原音专属密钥'), { target: { value: 'synthetic-only-key' } })
}
function seed(): void {
  store.execute({ kind: 'save', expectedRevision: 0, actionId: 'seed-action-0001', config: {
    purpose: PET_AUDIO_PURPOSE, baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'synthetic-model', apiKey: 'synthetic-only-key', enabled: false
  } })
}
it('rotated backend PIN after lost save returns to the existing runtime gate; re-unlock/read finds exactly one durable write', async () => {
  await open(); restart = true; loss = true
  const gate = vi.fn(); window.addEventListener('hsc-runtime-restarted', gate)
  try {
    fireEvent.click(screen.getByRole('button', { name: '保存原音配置' }))
    await waitFor(() => expect(writes).toBe(1))
    await waitFor(() => expect(gate).toHaveBeenCalledTimes(1))
    const raw = localStorage.getItem(marker)
    expect(raw).not.toBeNull()
    await localApi.parentUnlock('synthetic-pin', false)
    cleanup(); loss = false; store = new PetAudioSettingsStore(directory)
    render(<ParentPetAudioSettings childId="primary" />)
    await screen.findByText('原配置操作已找到，没有重新发送。')
    expect(store.execute({ kind: 'read' }).revision).toBe(1)
    expect(writes).toBe(1); expect(localStorage.getItem(marker)).toBeNull()
  } finally { window.removeEventListener('hsc-runtime-restarted', gate) }
})
it('serializes a slow durable read before clearing so an old configured result cannot overwrite the clear receipt', async () => {
  seed(); await open()
  const real = bridge.invoke.getMockImplementation()!
  let release!: () => void
  const blocked = new Promise<void>(resolve => { release = resolve })
  bridge.invoke.mockImplementation(async (...args: unknown[]) => {
    const result = await real(...args)
    if ((args[3] as PetAudioSettingsCommand)?.kind === 'read') await blocked
    return result
  })
  fireEvent.click(screen.getByRole('button', { name: '读取原配置' }))
  try { expect(screen.getByRole('button', { name: '清除原音密钥' })).toBeDisabled() }
  finally { await act(async () => release()) }
  fireEvent.click(screen.getByRole('button', { name: '清除原音密钥' }))
  await screen.findByText('原音密钥已清除，请核对服务应用状态；孩子原有用途记录保留。')
  expect(store.execute({ kind: 'read' })).toMatchObject({ revision: 2, configured: false })
})
it('does not discard a concurrent newer journal using the displayed older recovery action', async () => {
  localStorage.setItem(marker, JSON.stringify({ actionId: 'old-pending-action', expectedRevision: 0 }))
  render(<ParentPetAudioSettings childId="primary" />)
  await screen.findByText('尚未核实本机原操作；没有重复保存。')
  const newer = JSON.stringify({ actionId: 'new-pending-action', expectedRevision: 1 })
  localStorage.setItem(marker, newer)
  fireEvent.click(screen.getByRole('button', { name: '放弃本机配置标记' }))
  expect(localStorage.getItem(marker)).toBe(newer)
  expect(writes).toBe(0)
})
it('rejected save stays unconfirmed until read/discard; correcting the input creates only one durable revision', async () => {
  await open()
  fireEvent.change(screen.getByLabelText('原音服务地址'), { target: { value: 'https://untrusted.invalid/v1' } })
  fireEvent.click(screen.getByRole('button', { name: '保存原音配置' }))
  await screen.findByText(/保存未确认或配置被拒绝/)
  expect(writes).toBe(0); expect(store.execute({ kind: 'read' }).revision).toBe(0)
  const original = localStorage.getItem(marker)
  fireEvent.click(screen.getByRole('button', { name: '读取原配置' }))
  await screen.findByText('尚未核实本机原操作；没有重复保存。')
  expect(localStorage.getItem(marker)).toBe(original)
  expect(screen.getByRole('button', { name: '保存原音配置' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '放弃本机配置标记' }))
  fireEvent.click(screen.getByRole('button', { name: '读取原配置' }))
  await waitFor(() => expect(screen.getByRole('button', { name: '保存原音配置' })).toBeEnabled())
  fireEvent.change(screen.getByLabelText('原音专属密钥'), { target: { value: 'synthetic-only-key' } })
  fireEvent.click(screen.getByRole('button', { name: '保存原音配置' }))
  await screen.findByText('配置已加密保存，自动原音教学保持关闭。')
  expect(writes).toBe(1); expect(store.execute({ kind: 'read' }).revision).toBe(1)
})
it('same-runtime PIN expiration has an explicit re-unlock path, preserving the unknown operation identity', async () => {
  await open(); parent = 'parent-after'
  fireEvent.click(screen.getByRole('button', { name: '保存原音配置' }))
  await screen.findByText(/保存未确认或配置被拒绝/)
  const raw = localStorage.getItem(marker), gate = vi.fn()
  window.addEventListener('hsc-runtime-restarted', gate)
  try {
    fireEvent.click(screen.getByRole('button', { name: '重新解锁家长中心' }))
    expect(gate).toHaveBeenCalledTimes(1); expect(localStorage.getItem(marker)).toBe(raw)
    await localApi.parentUnlock('synthetic-pin', false)
    cleanup(); render(<ParentPetAudioSettings childId="primary" />)
    await screen.findByText('尚未核实本机原操作；没有重复保存。')
    expect(writes).toBe(0); expect(store.execute({ kind: 'read' }).revision).toBe(0)
  } finally { window.removeEventListener('hsc-runtime-restarted', gate) }
})
it('lost clear receipt recovers the tombstone after reopening, never writing a replacement configuration', async () => {
  seed(); await open(); loss = true
  fireEvent.click(screen.getByRole('button', { name: '清除原音密钥' }))
  await screen.findByText(/保存未确认或配置被拒绝/)
  const prior = store.execute({ kind: 'read' })
  expect(prior).toMatchObject({ revision: 2, configured: false, enabled: false })
  cleanup(); loss = false; store = new PetAudioSettingsStore(directory)
  render(<ParentPetAudioSettings childId="primary" />)
  await screen.findByText('原配置操作已找到，没有重新发送。')
  expect(store.execute({ kind: 'read' })).toEqual(prior)
  expect(writes).toBe(1); expect(screen.getByRole('button', { name: '清除原音密钥' })).toBeDisabled()
})
it('a failed current read removes the previous applied claim rather than keeping stale enabled status', async () => {
  vi.spyOn(Date, 'now').mockReturnValue(Date.parse(PET_AUDIO_TARIFF.verifiedAt) + 3600000)
  store.execute({ kind: 'save', expectedRevision: 0, actionId: 'seed-enabled-0001', config: {
    purpose: PET_AUDIO_PURPOSE, baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: PET_AUDIO_TARIFF.model,
    apiKey: 'synthetic-only-key', enabled: true, tariffId: PET_AUDIO_TARIFF.id
  } })
  await open(); await screen.findByText('已核验原音模型配置已应用，仍需当前孩子的用途许可。')
  parent = 'expired-parent'
  fireEvent.click(screen.getByRole('button', { name: '读取原配置' }))
  await screen.findByText('读取未完成，原记录未改变。')
  expect(screen.queryByText('已核验原音模型配置已应用，仍需当前孩子的用途许可。')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: '保存原音配置' })).toBeDisabled()
})
it('expired catalog read blocks enablement while preserving the encrypted revision and permitting clear', async () => {
  const clock = vi.spyOn(Date, 'now').mockReturnValue(Date.parse(PET_AUDIO_TARIFF.verifiedAt) + 3600000)
  store.execute({ kind: 'save', expectedRevision: 0, actionId: 'seed-enabled-0001', config: {
    purpose: PET_AUDIO_PURPOSE, baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: PET_AUDIO_TARIFF.model,
    apiKey: 'synthetic-only-key', enabled: true, tariffId: PET_AUDIO_TARIFF.id
  } })
  await open(); clock.mockReturnValue(Date.parse(PET_AUDIO_TARIFF.reviewBefore) + 3600000)
  fireEvent.click(screen.getByRole('button', { name: '读取原配置' }))
  await screen.findByText(/费率或地域核验已失效/)
  expect(screen.getByRole('checkbox', { name: '启用已核验原音配置' })).toBeDisabled()
  expect(store.execute({ kind: 'read' })).toMatchObject({ revision: 1, state: 'tariff_unavailable', enabled: false })
  fireEvent.click(screen.getByRole('button', { name: '清除原音密钥' }))
  await screen.findByText('原音密钥已清除，请核对服务应用状态；孩子原有用途记录保留。')
  expect(store.execute({ kind: 'read' })).toMatchObject({ revision: 2, configured: false })
})
it('switching children fences a late read and never consumes another child recovery marker', async () => {
  const other = parentPetPendingPrefix('junior') + 'original-audio-settings'
  localStorage.setItem(marker, JSON.stringify({ actionId: 'primary-pending-0001', expectedRevision: 0 }))
  const real = bridge.invoke.getMockImplementation()!
  let release!: () => void
  const blocked = new Promise<void>(resolve => { release = resolve })
  bridge.invoke.mockImplementation(async (...args: unknown[]) => {
    const result = await real(...args)
    if (args[1] === 'primary' && (args[3] as PetAudioSettingsCommand)?.kind === 'read') await blocked
    return result
  })
  const view = render(<ParentPetAudioSettings childId="primary" />)
  view.rerender(<ParentPetAudioSettings childId="junior" />)
  await waitFor(() => expect(screen.getByRole('button', { name: '保存原音配置' })).toBeEnabled())
  await act(async () => release())
  expect(screen.queryByRole('button', { name: '放弃本机配置标记' })).not.toBeInTheDocument()
  expect(localStorage.getItem(marker)).not.toBeNull(); expect(localStorage.getItem(other)).toBeNull()
  expect(writes).toBe(0)
})
it('malformed recovery data stays blocked but can be explicitly discarded and reread without a POST', async () => {
  localStorage.setItem(marker, '{broken')
  render(<ParentPetAudioSettings childId="primary" />)
  await screen.findByText('尚未核实本机原操作；没有重复保存。')
  expect(screen.getByRole('button', { name: '保存原音配置' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '放弃本机配置标记' }))
  fireEvent.click(screen.getByRole('button', { name: '读取原配置' }))
  await waitFor(() => expect(screen.getByRole('button', { name: '保存原音配置' })).toBeEnabled())
  expect(writes).toBe(0)
})
it('clearing a current enabled configuration also clears the enable checkbox instead of presenting a stale draft as enabled', async () => {
  vi.spyOn(Date, 'now').mockReturnValue(Date.parse(PET_AUDIO_TARIFF.verifiedAt) + 3600000)
  store.execute({ kind: 'save', expectedRevision: 0, actionId: 'seed-enabled-0001', config: {
    purpose: PET_AUDIO_PURPOSE, baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: PET_AUDIO_TARIFF.model,
    apiKey: 'synthetic-only-key', enabled: true, tariffId: PET_AUDIO_TARIFF.id
  } })
  await open()
  expect(screen.getByRole('checkbox', { name: '启用已核验原音配置' })).toBeChecked()
  fireEvent.click(screen.getByRole('button', { name: '清除原音密钥' }))
  await screen.findByText('原音密钥已清除，请核对服务应用状态；孩子原有用途记录保留。')
  expect(screen.getByRole('checkbox', { name: '启用已核验原音配置' })).not.toBeChecked()
})
it('late runtime inspection cannot clear a newer unlocked identity or close its screen', async () => {
  const real = bridge.invoke.getMockImplementation()!
  let release!: () => void, entered!: () => void, blockNext = true
  const blocked = new Promise<void>(resolve => { release = resolve }), started = new Promise<void>(resolve => { entered = resolve })
  bridge.invoke.mockImplementation(async (...args: unknown[]) => {
    const result = await real(...args)
    if (args[0] === 'runtime:get-config' && blockNext) { blockNext = false; entered(); await blocked }
    return result
  })
  const gate = vi.fn(); window.addEventListener('hsc-runtime-restarted', gate)
  const old = requestPetAudioSettings('primary', { kind: 'read' }).catch(error => error)
  try {
    await started
    resetRuntimeCache(); session = 'session-after'; parent = 'parent-after'
    await localApi.parentUnlock('synthetic-pin', false)
    release(); expect(await old).toBeInstanceOf(Error)
    expect(gate).not.toHaveBeenCalled()
    await expect(requestPetAudioSettings('junior', { kind: 'read' })).resolves.toMatchObject({ revision: 0 })
    expect(writes).toBe(0)
  } finally { release(); await old; window.removeEventListener('hsc-runtime-restarted', gate) }
})

it('selects the supported model only as an unsaved form change and persists it only after explicit Save', async () => {
  const retired = 'qwen3-omni-flash-2025-12-01'
  const existingKey = 'synthetic-existing-region-key'
  store.execute({ kind: 'save', expectedRevision: 0, actionId: 'retired-saved-configuration', config: {
    purpose: PET_AUDIO_PURPOSE, baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: retired,
    apiKey: existingKey, enabled: false, tariffId: 'aliyun-cn-qwen3-omni-20251201-20260911-v1'
  } })
  render(<ParentPetAudioSettings childId="primary" />)
  const select = await screen.findByRole('button', { name: '选用当前支持的型号' })
  expect(screen.getByLabelText('原音模型名称')).toHaveValue(retired)
  expect(store.execute({ kind: 'read' })).toMatchObject({ revision: 1, model: retired })
  expect(writes).toBe(0)
  expect(select).toHaveAttribute('type', 'button')
  const beforeIPC = bridge.invoke.mock.calls.length
  fireEvent.click(select)
  expect(screen.getByLabelText('原音模型名称')).toHaveValue(PET_AUDIO_TARIFF.model)
  expect(screen.getByText('已选用当前支持的型号，但尚未保存。请显式保存配置，再重新确认当前孩子的用途许可与预算。')).toBeInTheDocument()
  expect(bridge.invoke.mock.calls).toHaveLength(beforeIPC)
  expect(writes).toBe(0)
  expect(store.execute({ kind: 'read' })).toMatchObject({ revision: 1, model: retired })
  expect(screen.getByLabelText('原音专属密钥')).toHaveValue('')
  expect(screen.getByRole('checkbox', { name: '启用已核验原音配置' })).not.toBeChecked()
  fireEvent.click(screen.getByRole('button', { name: '保存原音配置' }))
  await screen.findByText('配置已加密保存，自动原音教学保持关闭。')
  expect(writes).toBe(1)
  const reopened = new PetAudioSettingsStore(directory)
  expect(reopened.execute({ kind: 'read' })).toMatchObject({ revision: 2, model: PET_AUDIO_TARIFF.model, enabled: false })
  expect(reopened.runtimeConfig()).toMatchObject({ model: PET_AUDIO_TARIFF.model, apiKey: existingKey,
    tariffId: PET_AUDIO_TARIFF.id, enabled: false })
  const changes = bridge.invoke.mock.calls.filter(row => row[0] === 'pet-audio:settings' && (row[3] as PetAudioSettingsCommand)?.kind !== 'read')
  expect(changes).toHaveLength(1)
  expect(changes[0][3]).toMatchObject({ kind: 'save', expectedRevision: 1, config: { model: PET_AUDIO_TARIFF.model, apiKey: '' } })
})
