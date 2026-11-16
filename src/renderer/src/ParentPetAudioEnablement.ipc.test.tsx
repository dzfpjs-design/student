import { realpathSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import type { PetAudioSettingsCommand } from '../../shared/petAudioSettings'
const bridge = vi.hoisted(() => ({ api: null as unknown, invoke: vi.fn() }))
vi.mock('electron', () => ({ app: { getPath: () => '' }, contextBridge: { exposeInMainWorld: (_: string, api: unknown) => { bridge.api = api } },
  ipcRenderer: { invoke: (...args: unknown[]) => bridge.invoke(...args) },
  safeStorage: { isEncryptionAvailable: () => true,
    encryptString: (raw: string) => Buffer.from(Buffer.from(raw).toString('base64')),
    decryptString: (raw: Buffer) => Buffer.from(raw.toString(), 'base64').toString() }
}))
import '../../preload/index'
import { PetAudioSettingsStore, createPetAudioSettingsHandler } from '../../main/pet-audio-settings'
import { buildLocalServiceChildEnvironment } from '../../main/local-service'
import { localApi, resetRuntimeCache } from './api'
import { ParentPetAudioTeaching } from './ParentPetAudioTeaching'
import { PET_AUDIO_TARIFF } from '../../shared/petAudioTariffs'

it('parent accepts reviewed tariff, real IPC persists, launch environment is derived from reopened protected settings, and lost reply recovers without reapply', async () => {
  const exported=process.env.HSC_PET_SETTINGS_TEST_ENV_PATH
  const dir = mkdtempSync(join(realpathSync(exported ? dirname(exported) : tmpdir()), 'hsc-pet-enabled-ipc-'))
  let store = new PetAudioSettingsStore(dir), applies = 0, lost = true
  let applied: string | null = null, env: NodeJS.ProcessEnv = {}
  const runtime = {
    apply: async () => {
      store = new PetAudioSettingsStore(dir)
      const config = store.runtimeConfig()
      env = buildLocalServiceChildEnvironment({ HSC_PET_AUDIO_INPUT_SECOND_MICROS: '1', HSC_MODEL_API_KEY: 'synthetic-67f2352e6b9765b0fb81' }, {
        sessionToken: 'synthetic-session', dataEncryptionKey: 'synthetic-key', dataDir: dir, databasePath: join(dir, 'test.db'),
        localModelDir: dir, modelConfig: null, voiceConfig: null, petAudioConfig: config
      })
      applies++; applied = config?.actionId ?? null
    }, applied: (id: string | null) => applied === id
  }
  const handler = createPetAudioSettingsHandler(store, async (sender, child, token) => {
    if (sender !== 7 || child !== 'primary' || token !== 'synthetic-parent') throw Error('Denied')
  }, runtime)
  try {
    vi.spyOn(Date, 'now').mockReturnValue(Date.parse(PET_AUDIO_TARIFF.verifiedAt) + 3600000)
    localStorage.clear(); resetRuntimeCache(); window.desktop = bridge.api as NonNullable<typeof window.desktop>
    bridge.invoke.mockImplementation(async (channel: string, child: string, token: string, command: PetAudioSettingsCommand) => {
      if (channel === 'runtime:get-config') return { status: 'ready', localApiUrl: 'http://127.0.0.1:8765', sessionToken: 'synthetic-session' }
      if (channel !== 'pet-audio:settings') throw Error('Unexpected IPC')
      const result = await handler({ sender: { id: 7 } }, child, token, command)
      if (command.kind === 'save' && lost) throw Error('Synthetic receipt loss after apply')
      return result
    })
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (!url.endsWith('/v1/parent/unlock')) throw Error('No model network allowed')
      return new Response(JSON.stringify({ parent_token: 'synthetic-parent' }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }))
    await localApi.parentUnlock('synthetic-pin', false)
    const request = async <T,>(path: string): Promise<T> => (path.endsWith('authorization')
      ? { child_id: 'primary', configured: false, current: false, grant: null } : { child_id: 'primary', items: [] }) as T
    render(<ParentPetAudioTeaching childId="primary" request={request} />)
    fireEvent.click(screen.getByRole('button', { name: '配置原音模型' }))
    await waitFor(() => expect(screen.getByLabelText('原音模型名称')).toHaveValue(PET_AUDIO_TARIFF.model))
    fireEvent.change(screen.getByLabelText('原音专属密钥'), { target: { value: 'synthetic-pet-test-key' } })
    fireEvent.click(screen.getByRole('checkbox', { name: '启用已核验原音配置' }))
    fireEvent.click(screen.getByRole('button', { name: '保存原音配置' }))
    await screen.findByText(/保存未确认或配置被拒绝/)
    expect(applies).toBe(1)
    expect(env.HSC_PET_AUDIO_ENABLED).toBe('true')
    expect(env.HSC_PET_AUDIO_MODEL).toBe(PET_AUDIO_TARIFF.model)
    expect(env.HSC_PET_AUDIO_INPUT_SECOND_MICROS).toBe('371')
    expect(env.HSC_MODEL_API_KEY).toBeUndefined()
    expect(readFileSync(join(dir, 'pet-audio-settings.bin')).toString()).not.toContain('synthetic-pet-test-key')
    lost = false
    fireEvent.click(screen.getByRole('button', { name: '读取原配置' }))
    await screen.findByText('原配置操作已找到，没有重新发送。')
    expect(applies).toBe(1)
    expect(screen.getByRole('checkbox', { name: '允许原音练习建议' })).toBeDisabled()
    expect(fetch).toHaveBeenCalledTimes(1)
    if (process.env.HSC_PET_SETTINGS_TEST_ENV_PATH) writeFileSync(process.env.HSC_PET_SETTINGS_TEST_ENV_PATH,
      JSON.stringify({ environment: Object.fromEntries(Object.entries(env).filter(([key]) => key.startsWith('HSC_PET_AUDIO_'))),
        synthetic: true, applies, revision: store.execute({ kind: 'read' }).revision }), { mode: 0o600 })
  } finally { cleanup(); resetRuntimeCache(); vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear(); if (!exported) rmSync(dir, { recursive: true, force: true }) }
})
