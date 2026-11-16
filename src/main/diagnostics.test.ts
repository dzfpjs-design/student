import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { afterEach, describe, expect, it } from 'vitest'
import { DiagnosticRecorder, sanitizeDiagnosticValue } from './diagnostics'

it('accepts only fixed native connection stages, categories and numeric generations', () => {
  expect(sanitizeDiagnosticValue({native_connect_stage:'downlink',native_connect_failure:'permission_denied',connection_generation:3}))
    .toEqual({native_connect_stage:'downlink',native_connect_failure:'permission_denied',connection_generation:3})
  expect(sanitizeDiagnosticValue({native_connect_stage:'playback_barrier',native_connect_failure:'playback_tail_unconfirmed',reason_code:'QWEN_PLAYBACK_TAIL_UNCONFIRMED'}))
    .toEqual({native_connect_stage:'playback_barrier',native_connect_failure:'playback_tail_unconfirmed',reason_code:'QWEN_PLAYBACK_TAIL_UNCONFIRMED'})
  expect(sanitizeDiagnosticValue({native_connect_stage:'PRIVATE_SOURCE',native_connect_failure:'PRIVATE_ERROR',connection_generation:'PRIVATE_ID',message:'PRIVATE_SPEECH'})).toEqual({})
})

it('writes established native socket termination metadata without raw error or close text', async () => {
  const root = await mkdtemp(join(tmpdir(), 'hsc-established-socket-diagnostic-'))
  try {
    const recorder = new DiagnosticRecorder(root)
    const details = { socket_end_kind: 'remote_close', socket_close_category: 'service_restart', elapsed_ms: 301_224,
      socket_application_idle_ms: 299_832 }
    await recorder.record({ source: 'main', event: 'voice.realtime.socket_ended',
      details: { ...details, message: 'PRIVATE_CLOSE_TEXT', endpoint: 'PRIVATE_HOST', authorization: 'synthetic-888d6efe70a5cb12b292' } })
    await recorder.record({ source: 'renderer', event: 'voice.realtime.keepalive_acknowledged',
      details: { runtime: 'qwen_omni_realtime', elapsed_ms: 23, message: 'PRIVATE_ACK_BODY' } })
    const raw = await readFile(recorder.logPath, 'utf8')
    const rows = raw.trim().split('\n').map(line => JSON.parse(line))
    expect(rows[0]).toMatchObject({ event: 'voice.realtime.socket_ended', details })
    expect(rows[1]).toMatchObject({ event: 'voice.realtime.keepalive_acknowledged', details: { runtime: 'qwen_omni_realtime', elapsed_ms: 23 } })
    expect(raw).not.toContain('PRIVATE_')
    expect(sanitizeDiagnosticValue({ socket_end_kind: 'PRIVATE_KIND', socket_close_category: 'PRIVATE_CODE',
      socket_transport_category: 'PRIVATE_ERROR', socket_application_idle_ms: -1 })).toEqual({})
  } finally { await rm(root, { recursive: true, force: true }) }
})

describe('finite wake control metadata', () => {
  it('persists the three exact local attention events without wake names or raw content', async () => {
    const root = await mkdtemp(join(tmpdir(), 'hsc-wake-control-diagnostic-'))
    const events = ['voice.wake.control_started', 'voice.wake.control_result', 'voice.wake.detected']
    const details = { mode: 'wake', local_only: true, woke: false, elapsed_ms: 42, audio_bytes: 16044 }
    try {
      const recorder = new DiagnosticRecorder(root)
      for (const event of events) {
        await recorder.record({ source: 'renderer', event, details: { ...details,
          wake_word: 'PRIVATE_WAKE_NAME', transcript: 'PRIVATE_SPEECH', text: 'PRIVATE_SPEECH',
          name: 'PRIVATE_WAKE_NAME', audio: 'PRIVATE_AUDIO', state: 'PRIVATE_STATE' } })
      }
      await recorder.record({ source: 'renderer', event: 'voice.wake.PRIVATE_WAKE_NAME',
        details: { mode: 'PRIVATE_MODE', wake_word: 'PRIVATE_WAKE_NAME' } })
      const raw = await readFile(recorder.logPath, 'utf8')
      const rows = raw.trim().split('\n').map(line => JSON.parse(line))
      expect(rows.slice(0, 3).map(row => row.event)).toEqual(events)
      expect(rows.slice(0, 3).map(row => row.details)).toEqual(events.map(() => details))
      expect(rows[3]).toMatchObject({ event: 'unregistered_event', details: {} })
      expect(raw).not.toContain('PRIVATE_')
    } finally { await rm(root, { recursive: true, force: true }) }
  })
})

describe('finite speech arbitration metadata', () => {
  const dispositions = ['finished', 'unavailable', 'cancelled', 'expired', 'superseded', 'full']
  const priorities = ['reminder', 'dialogue', 'lesson', 'urgent']
  it.each(dispositions)('retains arbitration disposition %s with only the four known priorities', (disposition) => {
    for (const priority of priorities) {
      expect(sanitizeDiagnosticValue({ disposition, priority, transcript: 'PRIVATE_SPEECH', message: 'synthetic-888d6efe70a5cb12b292' }))
        .toEqual({ disposition, priority })
    }
  })
  it('rejects arbitrary or wrong-field values instead of accepting a shared prefix', () => {
    for (const value of ['cancelled_PRIVATE_SPEECH', 'urgent_synthetic-888d6efe70a5cb12b292', 'PRIVATE_SPEECH', 1, true, null]) {
      expect(sanitizeDiagnosticValue({ disposition: value, priority: value })).toEqual({})
    }
    expect(sanitizeDiagnosticValue({ disposition: 'urgent', priority: 'cancelled' })).toEqual({})
  })
  it('persists useful arbitration outcomes in JSONL without retaining private content', async () => {
    const root = await mkdtemp(join(tmpdir(), 'hsc-arbitration-diagnostic-'))
    try {
      const recorder = new DiagnosticRecorder(root)
      for (const disposition of dispositions) {
        for (const priority of priorities) {
          await recorder.record({ source: 'renderer', event: 'voice.tts.arbitration', level: 'info',
            details: { disposition, priority, transcript: 'PRIVATE_SPEECH', message: 'synthetic-888d6efe70a5cb12b292' } })
        }
      }
      const raw = await readFile(recorder.logPath, 'utf8')
      expect(raw).not.toContain('PRIVATE_')
      expect(raw.trim().split('\n').map(line => JSON.parse(line).details))
        .toEqual(dispositions.flatMap(disposition => priorities.map(priority => ({ disposition, priority }))))
    } finally { await rm(root, { recursive: true, force: true }) }
  })
})

describe('finite ASR failure metadata', () => {
  const reasons = [
    'ASR_CLOUD_DIALOGUE_UNAVAILABLE', 'ASR_MODEL_NOT_READY', 'ASR_CLIENT_TIMEOUT',
    'ASR_CLIENT_NETWORK_OR_PROTOCOL', 'ASR_CLIENT_UNKNOWN',
  ]
  it.each(reasons)('retains %s without admitting speech or arbitrary client details', (reason_code) => {
    expect(sanitizeDiagnosticValue({ mode: 'conversation', reason_code, failure_kind: 'api',
      transcript: 'PRIVATE_SPEECH', message: 'synthetic-888d6efe70a5cb12b292', audio: 'PRIVATE_AUDIO' }))
      .toEqual({ mode: 'conversation', reason_code, failure_kind: 'api' })
    expect(sanitizeDiagnosticValue({ reason_code: 'ASR_CLIENT_synthetic-888d6efe70a5cb12b292', failure_kind: 'PRIVATE_SPEECH' }))
      .toEqual({})
  })
  it.each(['api', 'timeout', 'network_or_protocol', 'client'])('retains the closed failure kind %s', (failure_kind) => {
    expect(sanitizeDiagnosticValue({ failure_kind })).toEqual({ failure_kind })
  })
  it('persists the actual ASR cause but drops private error text', async () => {
    const root = await mkdtemp(join(tmpdir(), 'hsc-asr-diagnostic-'))
    try {
      const recorder = new DiagnosticRecorder(root)
      await recorder.record({ source: 'renderer', event: 'voice.asr.failed', level: 'error',
        details: { mode: 'conversation', reason_code: 'ASR_CLOUD_DIALOGUE_UNAVAILABLE', failure_kind: 'api',
          transcript: 'PRIVATE_SPEECH', message: 'synthetic-888d6efe70a5cb12b292' } })
      const text = await readFile(recorder.logPath, 'utf8')
      expect(JSON.parse(text)).toMatchObject({ event: 'voice.asr.failed', level: 'error',
        details: { mode: 'conversation', reason_code: 'ASR_CLOUD_DIALOGUE_UNAVAILABLE', failure_kind: 'api' } })
      expect(text).not.toContain('PRIVATE_')
    } finally { await rm(root, { recursive: true, force: true }) }
  })
})

describe('voice media observation metadata', () => {
  it('preserves only bounded build identity needed to match a diagnostic package', () => {
    expect(sanitizeDiagnosticValue({ version: '0.1.0', platform: 'win32', architecture: 'x64', packaged: true,
      installPath: 'PRIVATE_PATH', apiKey: 'synthetic-888d6efe70a5cb12b292' })).toEqual({
      version: '0.1.0', platform: 'win32', architecture: 'x64', packaged: true,
    })
    expect(sanitizeDiagnosticValue({ version: '0.1.0-synthetic-888d6efe70a5cb12b292', platform: 'PRIVATE_OS',
      architecture: 'PRIVATE_ID', packaged: 'PRIVATE_VALUE' })).toEqual({})
    expect(sanitizeDiagnosticValue({ error_code: 'PILOT_RECIPIENT_REVOKED', message: 'PRIVATE_RECIPIENT' }))
      .toEqual({ error_code: 'PILOT_RECIPIENT_REVOKED' })
    expect(sanitizeDiagnosticValue({ error_code: 'PILOT_PRIVATE_RECIPIENT' })).toEqual({})
  })
  it('keeps voice handoff stages and registered mail routes without accepting raw paths or speech', () => {
    expect(sanitizeDiagnosticValue({ input_stage: 'callback_owner_checked', source_status: 'owner_changed',
      route_template: '/v1/pilot-test-digest/{delivery_date}/submit-email', transcript: 'PRIVATE_SPEECH' }))
      .toEqual({ input_stage: 'callback_owner_checked', source_status: 'owner_changed',
        route_template: '/v1/pilot-test-digest/{delivery_date}/submit-email' })
    expect(sanitizeDiagnosticValue({ route_template: '/v1/children/PRIVATE_CHILD/reports' })).toEqual({})
  })
  it('retains actionable SMTP outcomes across reopen without recording mailbox credentials or content', async () => {
    const root = await mkdtemp(join(tmpdir(), 'hsc-smtp-diagnostics-')); roots.push(root)
    const recorder = new DiagnosticRecorder(root)
    for (const code of ['SMTP_CREDENTIALS_INVALID', 'SMTP_DELIVERY_UNKNOWN', 'SMTP_UNAVAILABLE',
      'PILOT_DIGEST_CLAIM_HTTP_503', 'SMTP_SUBMIT_HTTP_502']) {
      await recorder.record({ source: 'main', event: 'pilot_email_delivery_failed', details: {
        error_code: code, transport: 'smtp', client: 'configured_smtp',
        password: 'synthetic-d65ac3a4abfaf09619c713b1', recipient: 'PRIVATE_MAILBOX', body: 'PRIVATE_REPORT',
      } })
    }
    const reopened = new DiagnosticRecorder(root)
    const rows = (await readFile(reopened.logPath, 'utf8')).trim().split('\n').map(line => JSON.parse(line))
    expect(rows.map(row => row.error_code)).toEqual(['SMTP_CREDENTIALS_INVALID', 'SMTP_DELIVERY_UNKNOWN',
      'SMTP_UNAVAILABLE', 'PILOT_DIGEST_CLAIM_HTTP_503', 'SMTP_SUBMIT_HTTP_502'])
    expect(rows.every(row => row.details.transport === 'smtp' && row.details.client === 'configured_smtp')).toBe(true)
    expect(JSON.stringify(rows)).not.toContain('PRIVATE_')
    expect(sanitizeDiagnosticValue({ error_code: 'SMTP_PRIVATE_AUTH_CODE' })).toEqual({})
    expect((await reopened.summary()).maxRotatedFiles).toBe(32)
  })
  it.each(['QWEN_REALTIME_INPUT_TRANSCRIPTION_FAILED', 'QWEN_REALTIME_INPUT_IDENTITY_CAPACITY',
    'QWEN_REALTIME_SESSION_EXPIRED', 'QWEN_REALTIME_INPUT_ACTIVITY_UNCONFIRMED',
    'QWEN_NATIVE_CREDENTIAL_REJECTED', 'QWEN_NATIVE_ACCESS_DENIED', 'QWEN_NATIVE_ACCOUNT_BALANCE'])('retains the actionable failure category %s without supplier text', reason_code => {
    expect(sanitizeDiagnosticValue({ reason_code, message: 'PRIVATE_PROVIDER_BODY', transcript: 'PRIVATE_WORDS' }))
      .toEqual({ reason_code })
  })
  it('retains only bounded socket field types and documented provider category', () => {
    expect(sanitizeDiagnosticValue({ socket_message_code_type:'string', socket_message_message_type:'null',
      socket_provider_category:'rate_limit', code:'PRIVATE_SUPPLIER_CODE', message:'PRIVATE_SUPPLIER_MESSAGE' }))
      .toEqual({ socket_message_code_type:'string', socket_message_message_type:'null', socket_provider_category:'rate_limit' })
    expect(sanitizeDiagnosticValue({ socket_message_code_type:'PRIVATE_CODE', socket_message_message_type:'PRIVATE_MESSAGE',
      socket_provider_category:'PRIVATE_CATEGORY' })).toEqual({})
  })
  it('retains bounded source decisions without leaking capture owners or original content', () => {
    for (const source_status of ['current', 'missing', 'unbound', 'owner_changed', 'source_changed']) {
      expect(sanitizeDiagnosticValue({ input_stage: 'dispatch', source_status,
        sessionId: 'PRIVATE_OWNER', child_id: 'PRIVATE_CHILD', transcript: 'PRIVATE_WORDS',
        reference: { question_reference: 'PRIVATE_SOURCE' }, localInputId: 'PRIVATE_ID' }))
        .toEqual({ input_stage: 'dispatch', source_status })
    }
    expect(sanitizeDiagnosticValue({ input_stage: 'PRIVATE_WORDS', source_status: 'PRIVATE_WORDS' })).toEqual({})
    expect(sanitizeDiagnosticValue({ reason_code: 'input_item_denied', transcript: 'PRIVATE_WORDS' }))
      .toEqual({ reason_code: 'input_item_denied' })
  })
  it('retains the measured media path without promoting it to physical audibility', () => {
    expect(sanitizeDiagnosticValue({ observation_kind: 'remote_pcm_with_active_player',
      physical_audibility_verified: false, first_audio_ms: 21, pcm: [0.1], transcript: 'private' })).toEqual({
      observation_kind: 'remote_pcm_with_active_player', physical_audibility_verified: false, first_audio_ms: 21,
    })
    expect(sanitizeDiagnosticValue({ observation_kind: 'local_pcm_scheduled', physical_audibility_verified: true })).toEqual({
      observation_kind: 'local_pcm_scheduled',
    })
    expect(sanitizeDiagnosticValue({ observation_kind: 'unverified private label' })).toEqual({})
  })
})

const roots: string[] = []

it('quit metadata registers all fixed stages and codec failure without private content', async () => {
 const root=await mkdtemp(join(tmpdir(),'hsc-quit-metadata-'));roots.push(root);const recorder=new DiagnosticRecorder(root)
 const stages=['process_closed','app_quit_service_closed','app_quit_diagnostics_flushed','app_quit_final_requested','app_quit_unload_prevented','app_quit_will_quit','app_quit_codec_disposed','draft_codec_dispose_failed']
 for(const event of stages)await recorder.record({source:'main',event,details:{error_code:'DRAFT_RECOVERY_IO',body:'PRIVATE_BODY',path:'PRIVATE_PATH',token:'PRIVATE_TOKEN',child_id:'PRIVATE_CHILD'}})
 await recorder.flush();const raw=await readFile(recorder.logPath,'utf8'),rows=raw.trim().split('\n').map(line=>JSON.parse(line))
 expect(rows.map(row=>row.event)).toEqual(stages);expect(rows.every(row=>row.error_code==='DRAFT_RECOVERY_IO')).toBe(true);expect(raw).not.toContain('PRIVATE_')
})

afterEach(async () => {
  await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })))
})

describe('diagnostic recorder', () => {
  it('persists public speech usage and fixed failures while excluding supplier identities and private content', async () => {
    const root = await mkdtemp(join(tmpdir(), 'hsc-public-usage-diagnostic-')); roots.push(root)
    const recorder = new DiagnosticRecorder(root)
    await recorder.record({ source: 'main', event: 'voice.public_reply.usage', details: {
      input_tokens: 14, output_tokens: 7, total_tokens: 21,
      responseId: 'private-response', transcript: 'private-text', pcmBase64: 'private-audio',
    } })
    await recorder.record({ source: 'main', event: 'voice.public_reply.failed', level: 'warn', details: {
      code: 'PUBLIC_REPLY_TEXT_CHANGED', message: 'private-error',
    } })
    await recorder.flush()
    const text = await readFile(recorder.logPath, 'utf8'), rows = text.trim().split('\n').map(row => JSON.parse(row))
    expect(rows[0]).toMatchObject({ event: 'voice.public_reply.usage', details: { input_tokens: 14, output_tokens: 7, total_tokens: 21 } })
    expect(rows[1]).toMatchObject({ event: 'voice.public_reply.failed', error_code: 'PUBLIC_REPLY_TEXT_CHANGED' })
    expect(text).not.toMatch(/private-|responseId|transcript|pcmBase64/)
    expect(sanitizeDiagnosticValue({ input_tokens: -1, output_tokens: 1.5, total_tokens: null })).toEqual({})
    expect(sanitizeDiagnosticValue({ code: 'PUBLIC_REPLY_PRIVATE_LEAK' })).toEqual({})
  })
  it('persists direct and recovered input routes without original speech or identifiers', async () => {
    const root = await mkdtemp(join(tmpdir(), 'hsc-direct-input-diagnostic-')); roots.push(root)
    const recorder = new DiagnosticRecorder(root)
    await recorder.record({ source: 'renderer', event: 'voice.realtime.committed_input_dispatch', level: 'info', details: {
      route: 'committed_input', runtime: 'qwen_omni_realtime', transcript: 'synthetic-private', voice_input_id: 'private-input',
    } })
    await recorder.flush()
    const text = await readFile(recorder.logPath, 'utf8')
    expect(JSON.parse(text.trim())).toMatchObject({ event: 'voice.realtime.committed_input_dispatch',
      details: { route: 'committed_input', runtime: 'qwen_omni_realtime' } })
    expect(text).not.toMatch(/synthetic-private|private-input|transcript|voice_input_id/)
    expect(sanitizeDiagnosticValue({ route: 'reconnect_recovery' })).toEqual({ route: 'reconnect_recovery' })
  })
  it('persists unconfirmed delivery across recorder reopen without private speech or a self-certified completion', async () => {
    const root = await mkdtemp(join(tmpdir(), 'hsc-uncertain-delivery-')); roots.push(root)
    const recorder = new DiagnosticRecorder(root)
    await recorder.record({ source: 'renderer', event: 'voice.realtime.reply_delivery', level: 'warn', details: {
      completion_verified: false, delivered: false, reason_code: 'QWEN_REALTIME_PLAYBACK_COMPLETION_UNVERIFIED',
      transcript: 'synthetic-private', pcm: [0.1],
    } })
    const reopened = new DiagnosticRecorder(root); await reopened.flush()
    const text = await readFile(reopened.logPath, 'utf8')
    expect(JSON.parse(text.trim())).toMatchObject({ level: 'warn', details: {
      completion_verified: false, delivered: false, reason_code: 'QWEN_REALTIME_PLAYBACK_COMPLETION_UNVERIFIED',
    } })
    expect(text).not.toMatch(/synthetic-private|transcript|pcm/)
    expect(sanitizeDiagnosticValue({ completion_verified: true })).toEqual({})
  })
  it('preserves realtime route and delivery timing without retaining the utterance', () => {
    expect(sanitizeDiagnosticValue({ route: 'realtime_tool', runtime: 'qwen_omni_realtime',
      first_audio_ms: 1300, agent_runtime_ms: 700, delivered: true, transcript: 'private' })).toEqual({
        route: 'realtime_tool', runtime: 'qwen_omni_realtime', first_audio_ms: 1300, agent_runtime_ms: 700, delivered: true,
      })
  })
  it('keeps only diagnostic metadata, including inside nested errors', () => {
    expect(sanitizeDiagnosticValue({
      apiKey: 'sk-synthetic-442fdde4374222c70fdf',
      error: { authorization: 'Bearer private-token', imageBase64: 'raw-image', code: 'MODEL_TIMEOUT', elapsed_ms: 42 }
    })).toEqual({
      error: { code: 'MODEL_TIMEOUT', elapsed_ms: 42 }
    })
  })

  it('does not retain private strings under innocuous or unrecognized field names', () => {
    const safe = sanitizeDiagnosticValue({
      transcript: 'SYNTHETIC_PRIVATE', child_response: 'SYNTHETIC_PRIVATE', parentToken: 'SYNTHETIC_PRIVATE',
      source_path: '/synthetic/family/private.png', message: 'data:image/png;base64,U1lOVEg=',
      details: { arbitrary: 'SYNTHETIC_PRIVATE', elapsed_ms: 17, audio_bytes: 800, code: 'APP_RESTARTED' },
      error: new Error('SYNTHETIC_PRIVATE /synthetic/family/private.png')
    })
    expect(JSON.stringify(safe)).not.toMatch(/SYNTHETIC|synthetic|base64|private\.png/)
    expect(safe).toMatchObject({ details: { elapsed_ms: 17, audio_bytes: 800, code: 'APP_RESTARTED' }, error: { name: 'Error' } })
  })

  it('keeps onboarding state enums but drops spoken grade and wake-word text', () => {
    expect(sanitizeDiagnosticValue({
      selection_method: 'spoken_grade', initialization_required: true, step: 'wake_practice',
      transcript: '我是四年级', wake_word: '小知',
    })).toEqual({ selection_method: 'spoken_grade', initialization_required: true, step: 'wake_practice' })
  })

  it('bounds objects, arrays, cycles and non-finite values', () => {
    const details: Record<string, unknown> = { elapsed_ms: NaN, details: null }
    details.details = details
    expect(() => sanitizeDiagnosticValue(details)).not.toThrow()
    expect(JSON.stringify(sanitizeDiagnosticValue(details)).length).toBeLessThan(1000)
    expect(sanitizeDiagnosticValue({ statusCode: Infinity, confidence: 0.7 })).toEqual({ confidence: 0.7 })
    expect(sanitizeDiagnosticValue(JSON.parse('{"constructor":"SYNTHETIC_PRIVATE","__proto__":{"private":"SYNTHETIC_PRIVATE"}}'))).toEqual({})
  })

  it('persists useful error codes and elapsed time without raw errors, then reopens safely', async () => {
    const root = await mkdtemp(join(tmpdir(), 'hsc-diagnostics-'))
    roots.push(root)
    const recorder = new DiagnosticRecorder(root)
    await recorder.record({ source: 'renderer', event: 'voice.asr.failed', message: 'SYNTHETIC_PRIVATE MODEL_TIMEOUT',
      correlationId: '51b20367-e585-44a7-b2dc-dea3809b34f3', details: { elapsed_ms: 123, error_code: 'MODEL_TIMEOUT' } })
    const saved = JSON.parse((await readFile(recorder.logPath, 'utf8')).trim())
    expect(saved).toMatchObject({ event: 'voice.asr.failed', error_code: 'MODEL_TIMEOUT', details: { elapsed_ms: 123 } })
    expect(saved.summary).toBeTruthy()
    expect(saved.recovery_action).toBeTruthy()
    expect(saved.correlation_id).toBe('51b20367-e585-44a7-b2dc-dea3809b34f3')
    expect(JSON.stringify(saved)).not.toContain('SYNTHETIC_PRIVATE')
    const restarted = new DiagnosticRecorder(root)
    await restarted.record({ source: 'renderer', event: 'SYNTHETIC_PRIVATE', correlationId: 'SYNTHETIC_PRIVATE' })
    const lines = (await readFile(recorder.logPath, 'utf8')).trim().split('\n')
    expect(lines).toHaveLength(2)
    expect(lines.join('')).not.toContain('SYNTHETIC_PRIVATE')
    expect((await restarted.summary()).existingLogsVerified).toBe(false)
  })

  it('keeps non-speech rejection as an informational reason instead of an error', async () => {
    const root = await mkdtemp(join(tmpdir(), 'hsc-diagnostics-'))
    roots.push(root)
    const recorder = new DiagnosticRecorder(root)
    await recorder.record({
      source: 'renderer', event: 'voice.asr.non_speech_discarded', level: 'info',
      details: {
        mode: 'wake', reason_code: 'ASR_NO_HUMAN_SPEECH',
        transcript: 'SYNTHETIC_PRIVATE', audio: 'SYNTHETIC_PRIVATE',
      },
    })
    const saved = JSON.parse((await readFile(recorder.logPath, 'utf8')).trim())
    expect(saved).toMatchObject({
      event: 'voice.asr.non_speech_discarded', level: 'info',
      details: { mode: 'wake', reason_code: 'ASR_NO_HUMAN_SPEECH' },
    })
    expect(saved).not.toHaveProperty('error_code')
    expect(JSON.stringify(saved)).not.toContain('SYNTHETIC_PRIVATE')
  })

  it('preserves realtime rotation and first-audio event names for field diagnosis', async () => {
    const root = await mkdtemp(join(tmpdir(), 'hsc-diagnostics-'))
    roots.push(root)
    const recorder = new DiagnosticRecorder(root)
    await recorder.record({ source: 'renderer', event: 'voice.realtime.session_rotated', level: 'info' })
    await recorder.record({ source: 'renderer', event: 'voice.realtime.reply_first_audio', level: 'info', details: { latency_ms: 321 } })
    const saved = (await readFile(recorder.logPath, 'utf8')).trim().split('\n').map((line) => JSON.parse(line))
    expect(saved.map((line) => line.event)).toEqual([
      'voice.realtime.session_rotated', 'voice.realtime.reply_first_audio',
    ])
    expect(saved[1].details).toEqual({ latency_ms: 321 })
  })

  it('preserves source admission and rejection events in the persisted diagnostic log', async () => {
    const root = await mkdtemp(join(tmpdir(), 'hsc-diagnostics-'))
    roots.push(root)
    const recorder = new DiagnosticRecorder(root)
    await recorder.record({ source: 'renderer', event: 'voice.realtime.input_source_checked',
      details: { input_stage: 'admission', source_status: 'unbound', transcript: 'PRIVATE_WORDS' } })
    await recorder.record({ source: 'renderer', event: 'voice.realtime.input_admitted',
      details: { transcript: 'PRIVATE_WORDS' } })
    await recorder.record({ source: 'renderer', event: 'voice.realtime.input_rejected',
      details: { reason_code: 'microphone_closed', transcript: 'PRIVATE_WORDS' } })
    const text = await readFile(recorder.logPath, 'utf8')
    const saved = text.trim().split('\n').map(line => JSON.parse(line))
    expect(saved.map(x => x.event)).toEqual(['voice.realtime.input_source_checked',
      'voice.realtime.input_admitted', 'voice.realtime.input_rejected'])
    expect(saved[0].details).toEqual({ input_stage: 'admission', source_status: 'unbound' })
    expect(saved[2].details).toEqual({ reason_code: 'microphone_closed' })
    expect(text).not.toContain('PRIVATE_WORDS')
  })

  it('persists retry exhaustion and pending count without raw input or supplier error content', async () => {
    const root = await mkdtemp(join(tmpdir(), 'hsc-diagnostics-'))
    roots.push(root)
    const recorder = new DiagnosticRecorder(root)
    await recorder.record({ source: 'renderer', event: 'voice.realtime.retry_exhausted', level: 'warn',
      details: { reason_code: 'QWEN_REALTIME_INPUT_TRANSCRIPTION_FAILED', attempts: 3, count: 1,
        raw_audio_or_video: false, input: 'PRIVATE_WORDS', supplier_error: 'PRIVATE_BODY' } })
    const text = await readFile(recorder.logPath, 'utf8')
    expect(JSON.parse(text)).toMatchObject({ event: 'voice.realtime.retry_exhausted',
      details: { reason_code: 'QWEN_REALTIME_INPUT_TRANSCRIPTION_FAILED', attempts: 3, count: 1, raw_audio_or_video: false } })
    expect(text).not.toContain('PRIVATE_')
  })

  it('writes bounded JSONL files and reports local-only privacy boundaries', async () => {
    const root = await mkdtemp(join(tmpdir(), 'hsc-diagnostics-'))
    roots.push(root)
    const recorder = new DiagnosticRecorder(root, 260, 2)
    for (let index = 0; index < 8; index += 1) {
      await recorder.record({ source: 'local_service', event: 'worker_line', message: `line-${index}-${'x'.repeat(90)}` })
    }
    const summary = await recorder.summary()
    const current = await readFile(summary.logPath, 'utf8')
    expect(JSON.parse(current.trim()).event).toBe('worker_line')
    expect(summary.rotatedLogCount).toBeGreaterThan(0)
    expect(summary.rotatedLogCount).toBeLessThanOrEqual(2)
    expect(summary.rawMediaRecorded).toBe(false)
    expect(summary.uploadEnabled).toBe(false)
  })
  it('persists the exact finite retry-scheduled tuple through the actual recorder without raw error or identities', async () => {
    const root = await mkdtemp(join(tmpdir(), 'hsc-retry-diagnostics-'))
    roots.push(root)
    const recorder = new DiagnosticRecorder(root)
    await recorder.record({ source: 'renderer', event: 'voice.realtime.retry_scheduled', level: 'warn',
      message: 'PRIVATE_ERROR_BODY', details: { trigger: 'lease_renewal', reason_code: 'QWEN_REALTIME_MICROPHONE',
        attempt: 1, retry_delay_ms: 1000, connection_generation: 1,
        transcript: 'PRIVATE_WORDS', sessionId: 'PRIVATE_SESSION', child_id: 'PRIVATE_CHILD', lease_id: 'PRIVATE_LEASE' } })
    const text = await readFile(recorder.logPath, 'utf8')
    const saved = JSON.parse(text)
    expect(saved.event).toBe('voice.realtime.retry_scheduled')
    expect(saved.privacy_policy_version).toBe('diagnostic_metadata_v2')
    expect(saved.details).toEqual({ trigger: 'lease_renewal', reason_code: 'QWEN_REALTIME_MICROPHONE',
      attempt: 1, retry_delay_ms: 1000, connection_generation: 1 })
    expect(text).not.toContain('PRIVATE_')
  })

  it('persists all three finite triggers and original 1-based retry-delay pairs', async () => {
    const root = await mkdtemp(join(tmpdir(), 'hsc-retry-diagnostics-')); roots.push(root)
    const recorder = new DiagnosticRecorder(root)
    for (const [index, trigger] of ['status_error', 'lease_renewal', 'connect_failed'].entries()) {
      const details = { trigger, reason_code: 'QWEN_REALTIME_CONNECTION_FAILED', attempt: index + 1,
        retry_delay_ms: [1000, 3000, 8000][index], connection_generation: index * 2 + 1 }
      await recorder.record({ source: 'renderer', event: 'voice.realtime.retry_scheduled', details })
    }
    const rows = (await readFile(recorder.logPath, 'utf8')).trim().split('\n').map(value => JSON.parse(value))
    expect(rows.map(value => value.event)).toEqual(Array(3).fill('voice.realtime.retry_scheduled'))
    expect(rows.map(value => [value.details.trigger, value.details.attempt, value.details.retry_delay_ms, value.details.connection_generation]))
      .toEqual([['status_error', 1, 1000, 1], ['lease_renewal', 2, 3000, 3], ['connect_failed', 3, 8000, 5]])
  })

  it('drops unsafe generation, unsupported delays and private enum alternatives in actual persisted metadata', async () => {
    const root = await mkdtemp(join(tmpdir(), 'hsc-retry-diagnostics-')); roots.push(root)
    const recorder = new DiagnosticRecorder(root)
    const bad = [
      ...[-1, 0, 0.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1, '1', null].map(connection_generation => ({ connection_generation })),
      ...[-1, 0, 999, 1001, 1.5, Infinity, '1000'].map(retry_delay_ms => ({ retry_delay_ms })),
      { trigger: 'PRIVATE_ERROR_BODY', reason_code: 'PRIVATE_REASON', transcript: 'PRIVATE_ASR', item_id: 'PRIVATE_ITEM',
        owner: 'PRIVATE_OWNER', sessionId: 'PRIVATE_SESSION', child_id: 'PRIVATE_CHILD', lease_id: 'PRIVATE_LEASE',
        source_path: 'PRIVATE_MEDIA', audio: [1, 2], message: 'PRIVATE_PROVIDER_BODY' },
    ]
    for (const details of bad) await recorder.record({ source: 'renderer', event: 'voice.realtime.retry_scheduled', details })
    const text = await readFile(recorder.logPath, 'utf8')
    const rows = text.trim().split('\n').map(value => JSON.parse(value))
    expect(rows).toHaveLength(bad.length)
    expect(rows.every(value => value.event === 'voice.realtime.retry_scheduled' && Object.keys(value.details).length === 0)).toBe(true)
    expect(text).not.toContain('PRIVATE_')
    expect(sanitizeDiagnosticValue({ connection_generation: Number.MAX_SAFE_INTEGER })).toEqual({ connection_generation: Number.MAX_SAFE_INTEGER })
    expect(sanitizeDiagnosticValue({ attempt: 0, attempts: 3, delayMs: 250 })).toEqual({ attempt: 0, attempts: 3, delayMs: 250 })
  })

})
