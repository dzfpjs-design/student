import { execFileSync } from 'node:child_process'
import { mkdtemp, readFile, realpath, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { expect, it } from 'vitest'
import { DiagnosticRecorder } from './diagnostics'
import { classifyQwenRealtimeError } from '../renderer/src/QwenRealtimeVoiceInput'

it('exports reviewed playback failures from renderer classification through real recorder and daily ZIP, without private fields', async () => {
  const failures = [
    '实时语音播放器异常。', '实时语音播放观测不可用，不能确认回复已送达。',
    '实时语音播放观测暂时不可用，正在恢复连接。', '保守收音路径无法确认播放观测健康，正在恢复连接。',
    '保守收音路径缺少播放观测，正在恢复连接。', '保守收音路径的播放观测不可用，正在恢复连接。',
    '实时语音播放被系统权限阻止。', '实时语音播放格式或设备不受支持。', '实时语音播放被中断。',
    '实时语音播放状态无效。', '实时语音播放器启动失败。',
  ]
  const codes = failures.map(classifyQwenRealtimeError)
  expect(new Set(codes).size).toBe(failures.length)
  const root = await mkdtemp(join(await realpath(tmpdir()), 'hsc-playback-export-'))
  try {
    const recorder = new DiagnosticRecorder(join(root, 'diagnostics'))
    for (const reason_code of codes) await recorder.record({ source: 'renderer', level: 'error', event: 'voice.realtime.runtime_error',
      details: { reason_code, transcript: 'PRIVATE_TRANSCRIPT', audio: 'PRIVATE_AUDIO' } })
    await recorder.record({ source: 'renderer', event: 'voice.realtime.runtime_error', details: { reason_code: 'QWEN_PLAYBACK_PRIVATE_SECRET' } })
    await recorder.record({ source: 'main', level: 'warn', event: 'voice.realtime.socket_open_failed',
      details: { socket_open_stage: 'handshake', socket_open_failure: 'http_rejected', socket_open_retirement: 'provider_error', socket_payload_failure: 'message_structure', socket_message_top: 'object', socket_message_type: 'missing', socket_message_header: 'object', socket_message_payload: 'missing', socket_message_error: 'other', socket_message_code: 'other', socket_message_message: 'other', socket_message_event: 'object', url: 'PRIVATE_URL', header: 'PRIVATE_HEADER' } })
    await recorder.record({ source: 'main', level: 'warn', event: 'voice.realtime.socket_open_failed',
      details: { socket_open_stage: 'PRIVATE_STAGE', socket_open_failure: 'PRIVATE_ERROR', socket_open_retirement: 'PRIVATE_REASON', socket_payload_failure: 'PRIVATE_PAYLOAD', socket_message_top: 'PRIVATE_SHAPE' } })
    const raw = await readFile(recorder.logPath, 'utf8')
    expect(raw).not.toContain('PRIVATE_')
    expect(raw.trim().split('\n').slice(0, codes.length).map(line => JSON.parse(line).details.reason_code)).toEqual(codes)
    const script = [
      'from datetime import datetime,timedelta,timezone',
      'from io import BytesIO',
      'import json,sys,zipfile',
      'from home_study_agent.daily_diagnostic_bundle import build_daily_diagnostic_bundle',
      'now=datetime.now(timezone.utc)',
      'b=build_daily_diagnostic_bundle(sys.argv[1],start_utc=now-timedelta(hours=1),end_utc=now+timedelta(hours=1))',
      'z=zipfile.ZipFile(BytesIO(b.content))',
      'print(z.read("events.jsonl").decode())',
    ].join('\n')
    const exported = execFileSync(resolve('.venv/bin/python'), ['-c', script, root], {
      encoding: 'utf8', env: { ...process.env, PYTHONPATH: resolve('services/local_agent/src') }, timeout: 15000,
    })
    expect(exported).not.toContain('PRIVATE_')
    const rows = exported.trim().split('\n').map(line => JSON.parse(line))
    expect(rows.slice(0, codes.length).map(row => row.details.reason_code)).toEqual(codes)
    expect(rows.at(-3).details).toEqual({})
    expect(rows.at(-2).details).toEqual({ socket_open_stage: 'handshake', socket_open_failure: 'http_rejected', socket_open_retirement: 'provider_error', socket_payload_failure: 'message_structure', socket_message_top: 'object', socket_message_type: 'missing', socket_message_header: 'object', socket_message_payload: 'missing', socket_message_error: 'other', socket_message_code: 'other', socket_message_message: 'other', socket_message_event: 'object' })
    expect(rows.at(-1).details).toEqual({})
  } finally { await rm(root, { recursive: true, force: true }) }
})
