import { afterEach, expect, it, vi } from 'vitest'
import { localApi, resetRuntimeCache } from './api'

afterEach(() => { localStorage.clear(); resetRuntimeCache(); vi.unstubAllGlobals() })

it('sends the frozen pending evaluation id alongside the original ASR identity', async () => {
  resetRuntimeCache()
  vi.stubGlobal('desktop', undefined)
  const send = vi.fn().mockResolvedValue(new Response('{"agent_turn_id":"pending-control-turn"}', { status: 200 }))
  vi.stubGlobal('fetch', send)
  await localApi.agentTurn('session-a', '请暂停一下', 'original-playback', 'original-asr', 'evaluation-a')
  const body = JSON.parse(send.mock.calls[0][1].body)
  expect(body).toMatchObject({ transcript: '请暂停一下', pending_evaluation_turn_id: 'evaluation-a',
    voice_input_id: 'original-asr', playback_overlap_id: 'original-playback' })
  expect(typeof body.client_action_id).toBe('string')
})

it('does not add a pending id to an ordinary request', async () => {
  resetRuntimeCache()
  vi.stubGlobal('desktop', undefined)
  const send = vi.fn().mockResolvedValue(new Response('{"agent_turn_id":"ordinary-turn"}', { status: 200 }))
  vi.stubGlobal('fetch', send)
  await localApi.agentTurn('session-a', '今天的计划是什么')
  expect(JSON.parse(send.mock.calls[0][1].body)).not.toHaveProperty('pending_evaluation_turn_id')
})

it('sends independent recovery mode without scoring or playback identity', async () => {
  resetRuntimeCache()
  vi.stubGlobal('desktop', undefined)
  const send = vi.fn().mockResolvedValue(new Response('{"agent_turn_id":"recovery-control-turn"}', { status: 200 }))
  vi.stubGlobal('fetch', send)
  await localApi.agentTurn('session-a', '需要帮助', undefined, 'local-asr', undefined, true)
  const body = JSON.parse(send.mock.calls[0][1].body)
  expect(body).toMatchObject({ local_recovery_only: true, voice_input_id: 'local-asr' })
  expect(body).not.toHaveProperty('pending_evaluation_turn_id')
  expect(body).not.toHaveProperty('playback_overlap_id')
  await expect(localApi.agentTurn('session-a', '混合模式', undefined, 'local-asr', 'old-eval', true)).rejects.toThrow('LOCAL_RECOVERY_SCOPE_CONFLICT')
  expect(send).toHaveBeenCalledTimes(1)
})
