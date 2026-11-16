import { EventEmitter } from 'node:events'
import { mkdtempSync, realpathSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { expect, it, vi } from 'vitest'
const children = vi.hoisted(() => [] as Array<{ env: NodeJS.ProcessEnv; child: EventEmitter & { stdout: EventEmitter; stderr: EventEmitter; exitCode: number | null; kill: () => boolean } }>)
vi.mock('node:child_process', () => { const spawn = (_: string, __: unknown, options: { env: NodeJS.ProcessEnv }) => {
  const child = Object.assign(new EventEmitter(), { stdout: new EventEmitter(), stderr: new EventEmitter(), exitCode: null as number | null,
    kill: () => { child.exitCode = 0; child.emit('exit', 0, null);child.emit('close', 0, null); return true } })
  children.push({ env: options.env, child }); return child
}; return { spawn, default: { spawn } } })
vi.mock('electron', () => ({ app: { getVersion: () => '0.1.4', getPath: () => '/tmp/synthetic-pet-manager', getAppPath: () => process.cwd(), isPackaged: false } }))
import { LocalServiceManager } from './local-service'
import { PET_AUDIO_TARIFF } from '../shared/petAudioTariffs'
import { PET_AUDIO_PURPOSE } from '../shared/petAudioSettings'

it('managed restart passes only reviewed PET environment, tracks applied identity, and redacts the old child key while switching config', async () => {
  const directory=mkdtempSync(join(realpathSync(tmpdir()),'hsc-pet-manager-control-'))
  const before = process.env.HSC_LOCAL_AGENT_PYTHON, external = process.env.HSC_LOCAL_API_URL
  process.env.HSC_LOCAL_AGENT_PYTHON = process.execPath; delete process.env.HSC_LOCAL_API_URL
  const manager = new LocalServiceManager(), info = vi.spyOn(console, 'info').mockImplementation(() => undefined)
  vi.spyOn(Date, 'now').mockReturnValue(Date.parse(PET_AUDIO_TARIFF.verifiedAt) + 3600000)
  vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
    if (String(input).endsWith('/v1/runtime/shutdown')) children.at(-1)!.child.kill()
    return new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}', { status: 200 })
  })
  try {
    manager.setDataEncryptionKey('synthetic-encryption-key')
    const config = { purpose: PET_AUDIO_PURPOSE, apiKey: 'synthetic-original-private-key', baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
      model: PET_AUDIO_TARIFF.model, enabled: true, tariffId: PET_AUDIO_TARIFF.id, revision: 1, actionId: 'configuration-first',control:{file:join(directory,'pet-audio-catalog-control.bin'),state:'ABSENT'} }
    manager.setPetAudioConfig(config)
    expect(manager.petAudioConfigApplied(config.actionId)).toBe(false)
    await manager.start()
    expect(children[0].env.HSC_PET_AUDIO_API_KEY).toBe(config.apiKey)
    expect(manager.petAudioConfigApplied(config.actionId)).toBe(true)
    expect(JSON.stringify(manager.getRuntime())).not.toContain(config.apiKey)
    children[0].child.stdout.emit('data', Buffer.from(config.apiKey.slice(0, 15)))
    expect(info.mock.calls.flat().join('')).not.toContain(config.apiKey.slice(0, 15))
    children[0].child.stdout.emit('data', Buffer.from(config.apiKey.slice(15)))
    expect(info).toHaveBeenLastCalledWith(expect.not.stringContaining(config.apiKey))
    manager.setPetAudioConfig({ ...config, apiKey: 'synthetic-replacement-key', actionId: 'configuration-next', revision: 2 })
    children[0].child.stdout.emit('data', Buffer.from(config.apiKey))
    expect(info).toHaveBeenLastCalledWith(expect.not.stringContaining(config.apiKey))
    await manager.restart()
    expect(manager.petAudioConfigApplied('configuration-first')).toBe(false)
    expect(manager.petAudioConfigApplied('configuration-next')).toBe(true)
    expect(children[1].env.HSC_PET_AUDIO_API_KEY).toBe('synthetic-replacement-key')
    children[1].child.stdout.emit('data', Buffer.from('synthetic-replacement-key'))
    expect(info).toHaveBeenLastCalledWith(expect.not.stringContaining('synthetic-replacement-key'))
    manager.revokePetAudioConfig()
    expect(manager.petAudioConfigApplied('configuration-next')).toBe(false)
    expect(children).toHaveLength(2)
    expect(children[1].child.exitCode).toBeNull() // revocation does not cancel the existing sent request/process.
    await manager.restart()
    expect(children[2].env.HSC_PET_AUDIO_ENABLED).toBeUndefined()
  } finally {
    await manager.stop(); vi.restoreAllMocks(); children.length = 0;rmSync(directory,{recursive:true,force:true})
    if (before === undefined) delete process.env.HSC_LOCAL_AGENT_PYTHON; else process.env.HSC_LOCAL_AGENT_PYTHON = before
    if (external === undefined) delete process.env.HSC_LOCAL_API_URL; else process.env.HSC_LOCAL_API_URL = external
  }
})
