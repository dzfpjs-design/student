import { EventEmitter } from 'node:events'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const spawned = vi.hoisted(() => [] as Array<{ args: string[]; env: NodeJS.ProcessEnv; child: EventEmitter & { exitCode: number | null; stdout: EventEmitter; stderr: EventEmitter; kill: ReturnType<typeof vi.fn> } }>)

vi.mock('node:child_process', () => {
  const spawn = vi.fn((_executable: string, _args: string[], options: { env: NodeJS.ProcessEnv }) => {
    const child = Object.assign(new EventEmitter(), {
      exitCode: null as number | null,
      stdout: new EventEmitter(),
      stderr: new EventEmitter(),
      kill: vi.fn(() => {
        child.exitCode = 0
        child.emit('exit', 0, null);child.emit('close', 0, null)
        return true
      }),
    })
    spawned.push({ args:_args, env: options.env, child })
    return child
  })
  return { spawn, default: { spawn } }
})

// These tests replace the child process and health transport; avoid reserving a
// real local port merely to exercise their in-process lifecycle.
vi.mock('node:net', async (importOriginal) => {
 const original = await importOriginal<typeof import('node:net')>()
 const fakeCreateServer = () => {
  const server = new EventEmitter() as EventEmitter & { listen: (...args: any[]) => void; address: () => { port: number }; close: (done?: (error?: Error) => void) => void }
  server.listen = (_port, _host, done) => { queueMicrotask(done) }
  server.address = () => ({port: 28765})
  server.close = done => { if (done) queueMicrotask(() => done()) }
  return server
 }
 return { ...original, createServer: fakeCreateServer, default: {...original, createServer: fakeCreateServer} }
})

vi.mock('electron', () => ({
  app: {
    getAppPath: vi.fn(() => '/synthetic/project'),
    getVersion: vi.fn(() => '0.1.4'),
    getPath: vi.fn(() => '/tmp/hsc-local-service-test'),
    isPackaged: false
  }
}))

import {
  buildLocalServiceChildEnvironment,
  deriveDocumentVisionEnvironment,
  classifyLocalServiceOutputLevel,
  LOCAL_SERVICE_HEALTH_POLL_INTERVAL_MS,
  LOCAL_SERVICE_HEALTH_TIMEOUT_MS,
  LOCAL_SERVICE_RECOVERY_BASE_DELAY_MS,
  LOCAL_SERVICE_RECOVERY_MAX_ATTEMPTS,
  LocalServiceManager,
  localServiceHealthTimeout,
  resolveServiceWorkingDirectory
} from './local-service'
import { DiagnosticRecorder, sanitizeDiagnosticValue } from './diagnostics'

it('allows cold starts and database upgrades time to finish for the installed Windows worker only', () => {
  const installed = { platform: 'win32', packaged: true, managed: true, rotationAuthOnly: false } as const
  expect(localServiceHealthTimeout(installed)).toBe(240_000)
  expect(localServiceHealthTimeout({ ...installed, platform: 'darwin' })).toBe(LOCAL_SERVICE_HEALTH_TIMEOUT_MS)
  expect(localServiceHealthTimeout({ ...installed, packaged: false })).toBe(LOCAL_SERVICE_HEALTH_TIMEOUT_MS)
  expect(localServiceHealthTimeout({ ...installed, managed: false })).toBe(LOCAL_SERVICE_HEALTH_TIMEOUT_MS)
  expect(localServiceHealthTimeout({ ...installed, rotationAuthOnly: true })).toBe(LOCAL_SERVICE_HEALTH_TIMEOUT_MS)
})

describe('resolveServiceWorkingDirectory', () => {
  it('takes onboarding profile authority only from the desktop supplied userData path', () => {
    const child = buildLocalServiceChildEnvironment({ HSC_VOICE_ONBOARDING_PROFILE_DIR: '/untrusted/profile' }, {
      sessionToken: 'session-token', dataEncryptionKey: 'data-key', dataDir: '/profile/data', databasePath: '/profile/data/home-study.db',
      localModelDir: '/approved/models', modelConfig: null, voiceConfig: null, onboardingProfileDir: '/trusted/desktop-user-data',
    })
    expect(child.HSC_VOICE_ONBOARDING_PROFILE_DIR).toBe('/trusted/desktop-user-data')
    const absent = buildLocalServiceChildEnvironment({ HSC_VOICE_ONBOARDING_PROFILE_DIR: '/untrusted/profile' }, {
      sessionToken: 'session-token', dataEncryptionKey: 'data-key', dataDir: '/profile/data', databasePath: '/profile/data/home-study.db',
      localModelDir: '/approved/models', modelConfig: null, voiceConfig: null,
    })
    expect(absent.HSC_VOICE_ONBOARDING_PROFILE_DIR).toBeUndefined()
  })

  it('derives an explicitly bounded visual purpose only from same-origin safeStorage ASR settings', () => {
    const asrConfig = { provider: 'dashscope' as const, apiKey: 'synthetic-678021a8fe15143bcff6',
      baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen3-asr-flash' }
    const child = buildLocalServiceChildEnvironment({
      HSC_DOCUMENT_VISION_ENABLED: '1', HSC_DOCUMENT_VISION_API_KEY: 'synthetic-58d0f1473f58736dacf9',
      HSC_DOCUMENT_VISION_BASE_URL: 'https://evil.test/v1', HSC_DOCUMENT_VISION_DAILY_REQUEST_LIMIT: '999999'
    }, {
      sessionToken: 'session', dataEncryptionKey: 'data-key', dataDir: '/tmp/data', databasePath: '/tmp/data/test.db',
      localModelDir: '/tmp/models', modelConfig: null, voiceConfig: null, asrConfig
    })
    expect(child.HSC_DOCUMENT_VISION_ENABLED).toBe('1')
    expect(child.HSC_DOCUMENT_VISION_BASE_URL).toBe(asrConfig.baseUrl)
    expect(child.HSC_DOCUMENT_VISION_CREDENTIAL_SOURCE).toBe('asr_same_origin')
    expect(child.HSC_DOCUMENT_VISION_MODEL).toBe('qwen3-vl-flash')
    expect(child.HSC_DOCUMENT_VISION_TIMEOUT_SECONDS).toBe('60')
    expect(child.HSC_DOCUMENT_VISION_MAX_OUTPUT_TOKENS).toBe('4096')
    expect(child.HSC_DOCUMENT_VISION_DAILY_REQUEST_LIMIT).toBe('0')
    expect(child.HSC_DOCUMENT_VISION_DAILY_TOKEN_BUDGET).toBe('0')
    expect(child.HSC_DOCUMENT_VISION_MIN_INTERVAL_SECONDS).toBe('2')
    expect(child.HSC_DOCUMENT_VISION_API_KEY).toBeUndefined()
    expect(child.HSC_ASR_API_KEY).toBe('synthetic-678021a8fe15143bcff6')
    expect(deriveDocumentVisionEnvironment(asrConfig, { enabled: false })).toEqual({ HSC_DOCUMENT_VISION_ENABLED: '0' })
    expect(deriveDocumentVisionEnvironment(asrConfig, { model: 'qwen3-vl-flash', dailyRequestLimit: 20 })
      .HSC_DOCUMENT_VISION_DAILY_REQUEST_LIMIT).toBe('20')
  })

  it.each([
    'https://dashscope.aliyuncs.com/compatible-mode/v1',
    'https://dashscope-intl.aliyuncs.com/compatible-mode/v1',
    'https://workspace-12.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',
    'https://123.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1'
  ])('keeps the visual key on its configured region and workspace: %s', (baseUrl) => {
    const visual = deriveDocumentVisionEnvironment({ provider: 'dashscope', apiKey: 'test-asr-key', baseUrl, model: 'qwen3-asr-flash' })
    expect(visual.HSC_DOCUMENT_VISION_BASE_URL).toBe(baseUrl)
    expect(Object.keys(visual).some((key) => key.endsWith('API_KEY'))).toBe(false)
  })

  it.each([
    'https://asr.example/v1', 'http://dashscope.aliyuncs.com/compatible-mode/v1',
    'https://dashscope.aliyuncs.com.evil.test/compatible-mode/v1',
    'https://user@dashscope.aliyuncs.com/compatible-mode/v1',
    'https://dashscope.aliyuncs.com/compatible-mode/v1?next=evil',
    'https://dashscope.aliyuncs.com:8443/compatible-mode/v1',
    'https://api.deepseek.com'
  ])('never derives a visual route for an unapproved origin: %s', (baseUrl) => {
    expect(deriveDocumentVisionEnvironment({ provider: 'dashscope', apiKey: 'test-asr-key', baseUrl, model: 'qwen3-asr-flash' }))
      .toEqual({ HSC_DOCUMENT_VISION_ENABLED: '0' })
  })

  it('does not enable vision when ASR credentials have been cleared', () => {
    expect(deriveDocumentVisionEnvironment(null)).toEqual({ HSC_DOCUMENT_VISION_ENABLED: '0' })
    expect(deriveDocumentVisionEnvironment({ provider: 'dashscope', apiKey: '', baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen3-asr-flash' }))
      .toEqual({ HSC_DOCUMENT_VISION_ENABLED: '0' })
  })

  it('uses an independent vision key only on the already authorized ASR origin', () => {
    const config={provider:'dashscope' as const,apiKey:'synthetic-asr-key',documentVisionApiKey:'synthetic-vision-key',baseUrl:'https://dashscope.aliyuncs.com/compatible-mode/v1',model:'paraformer'}
    const visual=deriveDocumentVisionEnvironment(config)
    expect(visual.HSC_DOCUMENT_VISION_API_KEY).toBe('synthetic-vision-key')
    expect(visual.HSC_DOCUMENT_VISION_BASE_URL).toBe(config.baseUrl)
    expect(deriveDocumentVisionEnvironment({...config,baseUrl:'https://untrusted.invalid/v1'})).toEqual({HSC_DOCUMENT_VISION_ENABLED:'0'})
    expect(deriveDocumentVisionEnvironment(config,{enabled:false})).toEqual({HSC_DOCUMENT_VISION_ENABLED:'0'})
  })

  it('uses Python log prefixes instead of treating every stderr line as an error', () => {
    expect(classifyLocalServiceOutputLevel('INFO:     Application startup complete.', 'error')).toBe('info')
    expect(classifyLocalServiceOutputLevel('WARNING:  Slow request', 'error')).toBe('warn')
    expect(classifyLocalServiceOutputLevel('ERROR:    Worker crashed', 'info')).toBe('error')
    expect(classifyLocalServiceOutputLevel('Traceback (most recent call last):', 'error')).toBe('error')
  })

  it('allows a measured Windows cold start before declaring the agent unavailable', () => {
    expect(LOCAL_SERVICE_HEALTH_TIMEOUT_MS).toBeGreaterThanOrEqual(60_000)
    expect(LOCAL_SERVICE_HEALTH_POLL_INTERVAL_MS).toBeLessThanOrEqual(250)
  })

  it('uses the resources directory for a packaged Electron application', () => {
    expect(resolveServiceWorkingDirectory(true, 'C:\\app\\resources\\app.asar', 'C:\\app\\resources')).toBe(
      'C:\\app\\resources'
    )
  })

  it('uses the application directory while developing', () => {
    expect(resolveServiceWorkingDirectory(false, 'D:\\source', 'D:\\source\\resources')).toBe('D:\\source')
  })

  it('removes inherited provider and report-delivery credentials while preserving non-sensitive runtime variables', () => {
    const child = buildLocalServiceChildEnvironment({
      PATH: '/safe/bin', HSC_MODEL_API_KEY: 'synthetic-ec4b39dcec8fcfe9e265', HSC_FALLBACK_MODEL_API_KEY: 'synthetic-a19d9a3f7b428fecbace',
      HSC_ASR_API_KEY: 'synthetic-16149d1e7465e90e7b69',
      HSC_TTS_API_KEY: 'synthetic-be931c6d3e5016a882ea', HSC_REPORT_DELIVERY_TOKEN: 'host-report-secret',
      DASHSCOPE_API_KEY: 'synthetic-73c81a072742fae5353d', ANTHROPIC_API_KEY: 'synthetic-c39678f7cc0f330ab9f5',
      GOOGLE_API_KEY: 'synthetic-d0c3b1c42c00d44b8273', GEMINI_API_KEY: 'synthetic-2ee15881d967ec0d82ae',
      AZURE_OPENAI_API_KEY: 'synthetic-51969a1b36db199e6ac2', MISTRAL_API_KEY: 'synthetic-419822bb761b7ecd587c',
      COHERE_API_KEY: 'synthetic-087bc5e5b6140d96efb1', GROQ_API_KEY: 'synthetic-608d31f0ffd94275a8c8',
      TOGETHER_API_KEY: 'synthetic-50b7e18b3e6df8fc6bde', SILICONFLOW_API_KEY: 'synthetic-0035c364287e61018c46',
      MOONSHOT_API_KEY: 'synthetic-0417dc7e47119208cbe2', ZHIPUAI_API_KEY: 'synthetic-fc35ca5fd2e31df39bd2',
      BAIDU_API_KEY: 'synthetic-aca15f0e217ad3e77a56', VOLCENGINE_API_KEY: 'synthetic-9d565486cd256e71016a',
      ARK_API_KEY: 'synthetic-db20679f53a1591608fb', HF_TOKEN: 'host-hf-token', HUGGINGFACEHUB_API_TOKEN: 'host-hf-api-token',
      MINIMAX_API_KEY: 'synthetic-58a4f8065935b81d79e0', XAI_API_KEY: 'synthetic-445e66d531a502a6f1fa', PERPLEXITY_API_KEY: 'synthetic-66d03c6576732e0d7123',
      OPENROUTER_API_KEY: 'synthetic-03655e48733f0bd123c7', REPLICATE_API_TOKEN: 'host-replicate-token', FIREWORKS_API_KEY: 'synthetic-dd246207c4123aab989b',
      NVIDIA_API_KEY: 'synthetic-f6dd417337136fba9b1d', AWS_ACCESS_KEY_ID: 'host-aws-id', AWS_SECRET_ACCESS_KEY: 'host-aws-secret',
      AWS_SESSION_TOKEN: 'host-aws-token', AZURE_CLIENT_SECRET: 'synthetic-a049ded7407d73d9f176c460',
      GOOGLE_APPLICATION_CREDENTIALS: '/host/google-credentials.json', TENCENTCLOUD_SECRET_ID: 'host-tencent-id',
      TENCENTCLOUD_SECRET_KEY: 'synthetic-f2af38a9cd29217b7674',
      HSC_LOCAL_AGENT_PYTHON: '/approved/python', HSC_LOCAL_MODEL_DIR: '/approved/models',
      HSC_LOCAL_API_URL: 'http://127.0.0.1:18765', LANG: 'zh_CN.UTF-8', NODE_OPTIONS: '--max-old-space-size=512'
    }, {
      sessionToken: 'session-token', dataEncryptionKey: 'data-key', dataDir: '/profile/data', databasePath: '/profile/data/home-study.db',
      localModelDir: '/approved/models', modelConfig: null, voiceConfig: null
    })

    expect(child.PATH).toBe('/safe/bin')
    expect(child.HSC_LOCAL_AGENT_PYTHON).toBe('/approved/python')
    expect(child.HSC_LOCAL_MODEL_DIR).toBe('/approved/models')
    expect(child.HSC_LOCAL_API_URL).toBe('http://127.0.0.1:18765')
    expect(child.LANG).toBe('zh_CN.UTF-8')
    expect(child.NODE_OPTIONS).toBe('--max-old-space-size=512')
    expect(child.HSC_MODEL_API_KEY).toBeUndefined()
    expect(child.HSC_FALLBACK_MODEL_API_KEY).toBeUndefined()
    expect(child.HSC_ASR_API_KEY).toBeUndefined()
    expect(child.HSC_TTS_API_KEY).toBeUndefined()
    expect(child.HSC_REPORT_DELIVERY_TOKEN).toBeUndefined()
    expect(child.DASHSCOPE_API_KEY).toBeUndefined()
    for (const name of ['ANTHROPIC_API_KEY', 'GOOGLE_API_KEY', 'GEMINI_API_KEY', 'AZURE_OPENAI_API_KEY', 'MISTRAL_API_KEY', 'COHERE_API_KEY', 'GROQ_API_KEY', 'TOGETHER_API_KEY', 'SILICONFLOW_API_KEY', 'MOONSHOT_API_KEY', 'ZHIPUAI_API_KEY', 'BAIDU_API_KEY', 'VOLCENGINE_API_KEY', 'ARK_API_KEY', 'MINIMAX_API_KEY', 'XAI_API_KEY', 'PERPLEXITY_API_KEY', 'OPENROUTER_API_KEY', 'REPLICATE_API_TOKEN', 'FIREWORKS_API_KEY', 'NVIDIA_API_KEY', 'HF_TOKEN', 'HUGGINGFACEHUB_API_TOKEN', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AZURE_CLIENT_SECRET', 'GOOGLE_APPLICATION_CREDENTIALS', 'TENCENTCLOUD_SECRET_ID', 'TENCENTCLOUD_SECRET_KEY']) {
      expect(child[name]).toBeUndefined()
    }
  })

  it('injects only the encrypted desktop settings supplied to the manager', () => {
    const child = buildLocalServiceChildEnvironment({ HSC_MODEL_API_KEY: 'synthetic-ec4b39dcec8fcfe9e265', HSC_TTS_API_KEY: 'synthetic-be931c6d3e5016a882ea', HSC_VOICE_INTENT_API_KEY:'synthetic-847b7805820e6ceebda4' }, {
      sessionToken: 'session-token', dataEncryptionKey: 'data-key', dataDir: '/profile/data', databasePath: '/profile/data/home-study.db', localModelDir: '/approved/models',
      atomicGraphDbPath: '/approved/knowledge/atomic-knowledge.db',
      modelConfig: {
        provider: 'qwen', apiKey: 'synthetic-cc7b43595b216c93b66d', baseUrl: 'https://model.example/v1', model: 'desktop-model', voiceIntentModel: 'desktop-intent', voiceIntentApiKey:'synthetic-a46209e55cea6ac21c56',
        fallback: { provider: 'deepseek', apiKey: 'synthetic-add8552c843b0aa906bf', baseUrl: 'https://fallback.example/v1', model: 'desktop-fallback' }
      },
      asrConfig: { provider: 'dashscope', apiKey: 'synthetic-678021a8fe15143bcff6', baseUrl: 'https://asr.example/v1', model: 'qwen3-asr-flash' },
      voiceConfig: { provider: 'relay_pcm', apiKey: 'synthetic-184d412c0cb57321d055', relayUrl: 'https://voice.example/pcm', voiceId: 'adult-voice' }
    })

    expect(child.HSC_MODEL_API_KEY).toBe('synthetic-cc7b43595b216c93b66d')
    expect(child.HSC_VOICE_INTENT_API_KEY).toBe('synthetic-a46209e55cea6ac21c56')
    expect(child.HSC_FALLBACK_MODEL_API_KEY).toBe('synthetic-add8552c843b0aa906bf')
    expect(child.HSC_TTS_API_KEY).toBe('synthetic-184d412c0cb57321d055')
    expect(child.HSC_ASR_API_KEY).toBe('synthetic-678021a8fe15143bcff6')
    expect(child.HSC_ASR_BASE_URL).toBe('https://asr.example/v1')
    expect(child.HSC_ASR_MODEL).toBe('qwen3-asr-flash')
    expect(child.HSC_MODEL_PROVIDER).toBe('qwen')
    expect(child.HSC_LOCAL_MODEL_DIR).toBe('/approved/models')
    expect(child.HSC_ATOMIC_GRAPH_DB).toBe('/approved/knowledge/atomic-knowledge.db')
  })

  it('allows report credentials only when the safe-storage integration supplies them explicitly', () => {
    const child = buildLocalServiceChildEnvironment({
      HSC_REPORT_DELIVERY_EMAIL_PASSWORD: 'synthetic-fe4d3f0108ae9fae71a11b97',
      HSC_REPORT_DELIVERY_WECOM_WEBHOOK: 'https://host.invalid/webhook'
    }, {
      sessionToken: 'session-token', dataEncryptionKey: 'data-key', dataDir: '/profile/data', databasePath: '/profile/data/home-study.db',
      localModelDir: '/approved/models', modelConfig: null, voiceConfig: null,
      reportDeliverySecrets: { smtpPassword: 'synthetic-ee1fea55b53d625ea3ca', wecomWebhook: 'https://desktop.example/webhook' }
    })

    expect(child.HSC_REPORT_DELIVERY_EMAIL_PASSWORD).toBe('synthetic-ee1fea55b53d625ea3ca')
    expect(child.HSC_REPORT_DELIVERY_WECOM_WEBHOOK).toBe('https://desktop.example/webhook')
  })

  it('removes differently cased inherited credential aliases before injecting trusted settings', () => {
    const child = buildLocalServiceChildEnvironment({ hsc_model_api_key: 'synthetic-b40c68146e339a03e9c3', DashScope_Api_Key: 'synthetic-1c7fa9028ac0b8ca93a0',
      hSc_TtS_ApI_kEy: 'synthetic-59d417a9a43f2238c270', hsc_session_token: 'host-lower-session', hsc_data_encryption_key: 'host-lower-data',
      hsc_voice_onboarding_profile_dir: '/untrusted', hsc_voice_intent_api_key:'synthetic-17a418acf09ec94c12d46e33', LANG: 'zh_CN.UTF-8' }, {
      sessionToken:'trusted-session', dataEncryptionKey:'trusted-data', dataDir:'/profile',databasePath:'/profile/db',
      localModelDir:'/approved', modelConfig:null,voiceConfig:null,
    })
    for (const key of ['hsc_model_api_key','DashScope_Api_Key','hSc_TtS_ApI_kEy','hsc_session_token',
      'hsc_data_encryption_key','hsc_voice_onboarding_profile_dir','hsc_voice_intent_api_key']) expect(child[key]).toBeUndefined()
    expect(child.HSC_SESSION_TOKEN).toBe('trusted-session'); expect(child.HSC_DATA_ENCRYPTION_KEY).toBe('trusted-data')
    expect(child.LANG).toBe('zh_CN.UTF-8')
  })
})

describe('LocalServiceManager report-delivery secrets', () => {
  afterEach(() => { spawned.length = 0; vi.restoreAllMocks() })

  it('redacts overlapping and marker-like synthetic keys at every byte boundary', () => {
    const manager = new LocalServiceManager() as unknown as {
      createPetAudioLogRedactor: (keys: string[]) => {write: (bytes: Buffer) => string; end: () => string}
    }
    const keys = ['REDACTED', 'ABCDEFGHI', 'DEFGHIJKLM', 'ABCDEFGHIJKLM']
    for (const secret of keys) {
      const bytes = Buffer.from(`safe:${secret}:done`)
      for (let index=0; index<=bytes.length; index++) {
        const stream = manager.createPetAudioLogRedactor(keys)
        const output = stream.write(bytes.subarray(0,index)) + stream.write(bytes.subarray(index)) + stream.end()
        for (const key of keys) expect(output).not.toContain(key)
        expect(output).toContain('safe:'); expect(output).toContain(':done')
        expect(stream.end()).toBe('')
      }
    }
  })

  it('counts suppressed chunks without emitting their contents if no placeholder is safe', () => {
    const diagnostic = vi.fn()
    const manager = new LocalServiceManager(diagnostic) as unknown as {
      createPetAudioLogRedactor: (keys: string[]) => {write: (bytes: Buffer) => string; end: () => string}
    }
    const redactor = manager.createPetAudioLogRedactor(['SYNTHETIC[]*#~'])
    expect(redactor.write(Buffer.from('SYNTHETIC_PRIVATE_1'))).toBe('')
    expect(redactor.write(Buffer.from('SYNTHETIC_PRIVATE_2'))).toBe('')
    expect(redactor.end()).toBe(''); expect(redactor.end()).toBe('')
    expect(diagnostic).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
      event:'local_service.output_redaction_suppressed', level:'warn', details:{count:2},
    }))
    expect(JSON.stringify(diagnostic.mock.calls)).not.toContain('SYNTHETIC')
  })

  it('also checks fallback event names against every synthetic key', () => {
    const diagnostic = vi.fn()
    const manager = new LocalServiceManager(diagnostic) as unknown as {
      createPetAudioLogRedactor: (keys: string[]) => {write: (bytes: Buffer) => string; end: () => string}
    }
    const redactor = manager.createPetAudioLogRedactor(['local','stderr','SYNTHETIC[]*#~'])
    expect(redactor.write(Buffer.from('SYNTHETIC_PRIVATE'))).toBe('')
    redactor.end()
    expect(diagnostic).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({event:'stdout',details:{count:1}}))
    expect(JSON.stringify(diagnostic.mock.calls)).not.toContain('stderr')
    diagnostic.mockClear()
    const blocked = manager.createPetAudioLogRedactor(['local','stderr','stdout','SYNTHETIC[]*#~'])
    expect(blocked.write(Buffer.from('SYNTHETIC_PRIVATE'))).toBe('')
    expect(blocked.end()).toBe('')
    expect(diagnostic).not.toHaveBeenCalled()
  })

  it('presents a fixed actionable model classification failure without starting or retrying', async () => {
    const manager = new LocalServiceManager()
    manager.reportProtectedStartupFailure('rotation_model_unclassified')
    expect(manager.getRuntime()).toMatchObject({status:'error',error:expect.stringContaining('联系维护者核实模型')})
    expect(manager.getRuntime().nextRetryAt).toBeUndefined()
    await expect(manager.restart()).rejects.toThrow('联系维护者核实模型')
    expect(spawned).toHaveLength(0)
  })

  it('redacts distinct current and launched provider credentials across stdout and stderr chunks', async () => {
    const previousPython = process.env.HSC_LOCAL_AGENT_PYTHON
    process.env.HSC_LOCAL_AGENT_PYTHON = process.execPath
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      if (String(input).endsWith('/v1/runtime/shutdown')) {
        const child = spawned.at(-1)?.child
        if (child) { child.exitCode = 0; child.emit('exit', 0, null); child.emit('close', 0, null) }
      }
      return new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}', {status: 200})
    })
    const info = vi.spyOn(console, 'info').mockImplementation(() => undefined)
    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const root = await mkdtemp(join(tmpdir(), 'hsc-redaction-test-'))
    const recorder = new DiagnosticRecorder(root)
    const diagnostics = vi.fn(event => { void recorder.record({source:'local_service', ...event}) })
    const manager = new LocalServiceManager(diagnostics)
    const secrets = ['SYNTHETIC_PRIMARY_OLD_1', 'SYNTHETIC_FALLBACK_OLD_2', 'SYNTHETIC_ASR_OLD_3', 'SYNTHETIC_TTS_OLD_4', 'SYNTHETIC_DATA_OLD_5', 'REDACTED', 'SYNTHETIC_INTENT_OLD_7', 'SYNTHETIC_VISION_OLD_8']
    try {
      manager.setDataEncryptionKey(secrets[4])
      manager.setReportDeliverySecrets({smtpPassword: secrets[5]})
      manager.setModelConfig({provider:'qwen',apiKey:secrets[0],baseUrl:'https://model.invalid/v1',model:'qwen-test',voiceIntentModel:'qwen-test',voiceIntentApiKey:secrets[6],
        fallback:{provider:'deepseek',apiKey:secrets[1],baseUrl:'https://fallback.invalid/v1',model:'deepseek-test'}})
      manager.setAsrConfig({provider:'dashscope',apiKey:secrets[2],documentVisionApiKey:secrets[7],baseUrl:'https://dashscope.aliyuncs.com/compatible-mode/v1',model:'qwen3-asr-flash'})
      manager.setVoiceConfig({provider:'qwen_voice_clone',relayUrl:'',apiKey:secrets[3],voiceId:'synthetic-voice',model:'qwen3-tts-vc-2026-01-22',region:'cn-beijing'})
      await manager.start()
      const child = spawned[0].child
      // A concurrent settings change cannot make the still-running child's old secrets safe to print.
      manager.setModelConfig({provider:'qwen',apiKey:'SYNTHETIC_PRIMARY_NEW_6',baseUrl:'https://model.invalid/v1',model:'qwen-test',voiceIntentModel:'qwen-test'})
      manager.setAsrConfig({provider:'dashscope',apiKey:secrets[2],documentVisionApiKey:'SYNTHETIC_VISION_NEW_9',baseUrl:'https://dashscope.aliyuncs.com/compatible-mode/v1',model:'qwen3-asr-flash'})
      for (const secret of [...secrets, 'SYNTHETIC_PRIMARY_NEW_6', 'SYNTHETIC_VISION_NEW_9']) for (const stream of [child.stdout, child.stderr]) {
        stream.emit('data', Buffer.from('value=' + secret.slice(0, 8)))
        stream.emit('data', Buffer.from(secret.slice(8, 16)))
        stream.emit('data', Buffer.from(secret.slice(16) + '\n'))
      }
      const logs = JSON.stringify([info.mock.calls,error.mock.calls,diagnostics.mock.calls])
      for (const secret of [...secrets, 'SYNTHETIC_PRIMARY_NEW_6', 'SYNTHETIC_VISION_NEW_9']) {
        expect(logs).not.toContain(secret)
        expect(logs).not.toContain(secret.slice(0,8))
      }
      expect(logs).toContain('[PRIVATE]')
      const stdoutBeforeEnd = info.mock.calls.length
      child.stdout.emit('data', Buffer.from('safe-tail=SYNTHETI'))
      child.stdout.emit('end')
      child.stdout.emit('close')
      const stdoutTail = JSON.stringify(info.mock.calls.slice(stdoutBeforeEnd))
      expect(stdoutTail).toContain('safe-tail=')
      expect(stdoutTail).toContain('[PRIVATE_TAIL]')
      expect(stdoutTail).not.toContain('SYNTHETI')
      expect(stdoutTail.match(/PRIVATE_TAIL/g)).toHaveLength(1)
      const stderrBeforeEnd = error.mock.calls.length, unicode = Buffer.from('普通日志终止')
      child.stderr.emit('data', unicode.subarray(0, 2))
      child.stderr.emit('data', unicode.subarray(2))
      child.stderr.emit('end')
      expect(JSON.stringify(error.mock.calls.slice(stderrBeforeEnd))).toContain('普通日志终止')
      await recorder.flush()
      const persisted = await readFile(recorder.logPath, 'utf8')
      expect(persisted).not.toContain('SYNTHETI')
      await manager.stop()
    } finally {
      await recorder.flush()
      await rm(root, {recursive:true,force:true})
      if (previousPython === undefined) delete process.env.HSC_LOCAL_AGENT_PYTHON
      else process.env.HSC_LOCAL_AGENT_PYTHON = previousPython
    }
  })

  it('injects secrets into each new agent process, but not runtime summaries or forwarded logs', async () => {
    const previousPython = process.env.HSC_LOCAL_AGENT_PYTHON
    process.env.HSC_LOCAL_AGENT_PYTHON = process.execPath
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/v1/runtime/shutdown')) {
        const current = spawned.at(-1)?.child
        if (current) {
          current.exitCode = 0
          current.emit('exit', 0, null);current.emit('close', 0, null)
        }
      }
      return new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}', { status: 200 })
    })
    const info = vi.spyOn(console, 'info').mockImplementation(() => undefined)
    try {
      const manager = new LocalServiceManager()
      manager.setDataEncryptionKey('synthetic-data-key')
      manager.setReportDeliverySecrets({ smtpPassword: 'synthetic-smtp-secret', wecomWebhook: 'https://example.test/synthetic-webhook' })
      manager.setAsrConfig({ provider: 'dashscope', apiKey: 'synthetic-asr-secret',
        baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen3-asr-flash' })
      manager.setDocumentVisionConfig({ dailyRequestLimit: 30 })

      await manager.start()
      expect(spawned).toHaveLength(1)
      expect(spawned[0].env.HSC_REPORT_DELIVERY_EMAIL_PASSWORD).toBe('synthetic-smtp-secret')
      expect(spawned[0].env.HSC_REPORT_DELIVERY_WECOM_WEBHOOK).toBe('https://example.test/synthetic-webhook')
      expect(spawned[0].env.HSC_DOCUMENT_VISION_ENABLED).toBe('1')
      expect(spawned[0].env.HSC_DOCUMENT_VISION_DAILY_REQUEST_LIMIT).toBe('30')
      expect(JSON.stringify(manager.getRuntime())).not.toContain('synthetic-asr-secret')
      expect(JSON.stringify(manager.getRuntime())).not.toContain('synthetic-smtp-secret')
      expect(JSON.stringify(manager.getRuntime())).not.toContain('synthetic-webhook')
      spawned[0].child.stdout.emit('data', Buffer.from('password=synthetic-smtp-secret webhook=https://example.test/synthetic-webhook'))
      expect(info).toHaveBeenCalledWith(expect.not.stringContaining('synthetic-smtp-secret'))
      expect(info).toHaveBeenCalledWith(expect.not.stringContaining('synthetic-webhook'))
      expect(info).toHaveBeenCalledWith(expect.stringContaining('[REDACTED]'))
      spawned[0].child.stdout.emit('data', Buffer.from('Authorization: Bearer synthetic-asr-secret'))
      expect(info).toHaveBeenLastCalledWith(expect.not.stringContaining('synthetic-asr-secret'))

      await manager.restart()
      expect(spawned).toHaveLength(2)
      expect(spawned[1].env.HSC_REPORT_DELIVERY_EMAIL_PASSWORD).toBe('synthetic-smtp-secret')
      expect(spawned[1].env.HSC_REPORT_DELIVERY_WECOM_WEBHOOK).toBe('https://example.test/synthetic-webhook')
      expect(fetchMock).toHaveBeenCalled()
      await manager.stop()
    } finally {
      if (previousPython === undefined) delete process.env.HSC_LOCAL_AGENT_PYTHON
      else process.env.HSC_LOCAL_AGENT_PYTHON = previousPython
    }
  })
})

describe('LocalServiceManager unattended recovery', () => {
  const previousPython = process.env.HSC_LOCAL_AGENT_PYTHON

  beforeEach(() => {
    process.env.HSC_LOCAL_AGENT_PYTHON = process.execPath
  })

  afterEach(() => {
    vi.useRealTimers()
    spawned.length = 0
    vi.restoreAllMocks()
    if (previousPython === undefined) delete process.env.HSC_LOCAL_AGENT_PYTHON
    else process.env.HSC_LOCAL_AGENT_PYTHON = previousPython
  })

  it('reports protected pre-start failure without starting a process or exposing credentials', () => {
    const diagnostic = vi.fn()
    const manager = new LocalServiceManager(diagnostic)
    manager.reportProtectedStartupFailure()
    expect(manager.getRuntime()).toMatchObject({ status: 'error', error: expect.stringContaining('不会自动更换密钥') })
    expect(spawned).toHaveLength(0)
    expect(diagnostic).toHaveBeenCalledWith(expect.objectContaining({ event: 'protected_startup_failed' }))
    expect(manager.getRuntime().nextRetryAt).toBeUndefined()
  })

  it('starts only the owned PIN recovery mode without passing provider secrets and stops before leaving it', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      if (String(input).endsWith('/v1/runtime/shutdown')) {
        const child=spawned.at(-1)?.child
        if (child) { child.exitCode=0;child.emit('exit',0,null);child.emit('close',0,null) }
      }
      return new Response(JSON.stringify({build_component:'home-study-local-agent',build_version:'0.1.4',status:'rotation_auth_only',normal_business_ready:false}),{status:200})
    })
    const manager=new LocalServiceManager()
    manager.setDataEncryptionKey('synthetic-data-key')
    manager.setModelConfig({provider:'qwen',apiKey:'synthetic-model-key',baseUrl:'https://model.invalid/v1',model:'qwen-test',voiceIntentModel:'qwen-test'})
    manager.setAsrConfig({provider:'dashscope',apiKey:'synthetic-asr-key',documentVisionApiKey:'synthetic-vision-key',baseUrl:'https://dashscope.aliyuncs.com/compatible-mode/v1',model:'qwen3-asr-flash'})
    manager.setReportDeliverySecrets({smtpPassword:'synthetic-smtp-password'})
    await manager.startRotationAuthOnly()
    expect(manager.getRuntime()).toMatchObject({status:'ready',rotationAuthOnly:true})
    expect(()=>manager.admitCloudProviderRefresh()).toThrow('CLOUD_PROVIDER_REFRESH_BUSY')
    expect(spawned[0].args).toContain('--rotation-auth-only')
    const env=JSON.stringify(spawned[0].env)
    for (const secret of ['synthetic-model-key','synthetic-asr-key','synthetic-vision-key','synthetic-smtp-password']) expect(env).not.toContain(secret)
    await manager.stopRotationAuthOnly()
    expect(manager.getRuntime().rotationAuthOnly).toBeUndefined()
  })

  it('preserves a protected startup failure instead of bypassing it through UI restart', async () => {
    const diagnostic = vi.fn()
    const manager = new LocalServiceManager(diagnostic)
    manager.reportProtectedStartupFailure()
    const original = manager.getRuntime()
    await expect(manager.restart()).rejects.toThrow('不会自动更换密钥')
    expect(manager.getRuntime()).toEqual(original)
    expect(spawned).toHaveLength(0)
  })

  it('makes missing media key recovery actionable and blocks even an external runtime bypass', async () => {
    vi.stubEnv('HSC_LOCAL_API_URL', 'http://127.0.0.1:1')
    try {
      const manager = new LocalServiceManager()
      manager.reportProtectedStartupFailure('media_key_recovery')
      await expect(manager.start()).rejects.toThrow('恢复原有受保护密钥')
      expect(manager.getRuntime()).toMatchObject({ status: 'error', error: expect.stringContaining('不会删除原资料') })
      expect(spawned).toHaveLength(0)
    } finally {
      vi.unstubAllEnvs()
    }
  })

  it('recovers an unexpectedly exited local agent without user interaction', async () => {
    vi.useFakeTimers()
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}', { status: 200 }))
    const manager = new LocalServiceManager()
    manager.setDataEncryptionKey('synthetic-data-key')

    await manager.start()
    expect(manager.getRuntime().status).toBe('ready')
    spawned[0].child.exitCode = 7
    spawned[0].child.emit('exit', 7, null);spawned[0].child.emit('close', 7, null)

    expect(manager.getRuntime()).toMatchObject({ status: 'starting', recoveryAttempt: 1 })
    await vi.advanceTimersByTimeAsync(LOCAL_SERVICE_RECOVERY_BASE_DELAY_MS)
    expect(spawned).toHaveLength(2)
    expect(manager.getRuntime().status).toBe('ready')
    spawned[1].child.exitCode = 0
    spawned[1].child.emit('exit', 0, null);spawned[1].child.emit('close', 0, null)
    await manager.stop()
  })

  it('stops retrying after a bounded crash loop and leaves an actionable error', async () => {
    vi.useFakeTimers()
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}', { status: 200 }))
    const manager = new LocalServiceManager()
    manager.setDataEncryptionKey('synthetic-data-key')
    await manager.start()

    for (let failure = 0; failure <= LOCAL_SERVICE_RECOVERY_MAX_ATTEMPTS; failure += 1) {
      const child = spawned.at(-1)!.child
      child.exitCode = 9
      child.emit('exit', 9, null);child.emit('close', 9, null)
      if (failure < LOCAL_SERVICE_RECOVERY_MAX_ATTEMPTS) {
        const delay = Math.min(LOCAL_SERVICE_RECOVERY_BASE_DELAY_MS * (2 ** failure), 30_000)
        await vi.advanceTimersByTimeAsync(delay)
      }
    }

    expect(spawned).toHaveLength(LOCAL_SERVICE_RECOVERY_MAX_ATTEMPTS + 1)
    expect(manager.getRuntime().status).toBe('error')
    expect(manager.getRuntime().error).toContain('自动恢复已连续失败')
    await expect(manager.stop()).rejects.toMatchObject({ code: 'LOCAL_SERVICE_SHUTDOWN_ABNORMAL' })
    expect(manager.captureAbnormalShutdownAcknowledgement()?.()).toBe(true)
  })

  it('cancels a queued recovery when the desktop application is quitting', async () => {
    vi.useFakeTimers()
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}', { status: 200 }))
    const manager = new LocalServiceManager()
    manager.setDataEncryptionKey('synthetic-data-key')
    await manager.start()
    spawned[0].child.exitCode = 3
    spawned[0].child.emit('exit', 3, null);spawned[0].child.emit('close', 3, null)

    await expect(manager.stop()).rejects.toMatchObject({ code: 'LOCAL_SERVICE_SHUTDOWN_ABNORMAL' })
    expect(manager.captureAbnormalShutdownAcknowledgement()?.()).toBe(true)
    await vi.advanceTimersByTimeAsync(LOCAL_SERVICE_RECOVERY_BASE_DELAY_MS * 2)
    expect(spawned).toHaveLength(1)
    expect(manager.getRuntime().status).toBe('error')
  })

  it('coalesces concurrent startup requests into one local agent process', async () => {
    let resolveHealth: ((value: Response) => void) | undefined
    vi.spyOn(globalThis, 'fetch').mockImplementation(() => new Promise((resolve) => { resolveHealth = resolve }))
    const manager = new LocalServiceManager()
    manager.setDataEncryptionKey('synthetic-data-key')

    const first = manager.start()
    const second = manager.start()
    await vi.waitFor(() => expect(spawned).toHaveLength(1))
    resolveHealth?.(new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}', { status: 200 }))
    await Promise.all([first, second])
    expect(manager.getRuntime().status).toBe('ready')
    spawned[0].child.exitCode = 0
    spawned[0].child.emit('exit', 0, null);spawned[0].child.emit('close', 0, null)
    await manager.stop()
  })
})

it('cancels an older launch when a tutor endpoint is saved during startup and never credits it as applied', async () => {
  const previous = process.env.HSC_LOCAL_AGENT_PYTHON
  process.env.HSC_LOCAL_AGENT_PYTHON = process.execPath
  const manager = new LocalServiceManager()
  manager.setDataEncryptionKey('synthetic-data-key')
  manager.setTutorExplanationConfig({apiKey:'synthetic-old-key-123',baseUrl:'https://old.example.test/v1',model:'deepseek-test'})
  vi.spyOn(globalThis,'fetch').mockImplementation(async (input, init) => {
    if (String(input).endsWith('/v1/runtime/shutdown')) {
      const child=spawned.at(-1)?.child
      if (child && child.exitCode===null) {child.exitCode=0;child.emit('exit',0,null);child.emit('close',0,null)}
      return new Response('{}',{status:200})
    }
    return await new Promise<Response>((_resolve,reject)=>{
      const signal=init?.signal
      if(signal?.aborted) reject(new Error('aborted'))
      else signal?.addEventListener('abort',()=>reject(new Error('aborted')),{once:true})
    })
  })
  try {
    const oldRestart=manager.restart().catch(error=>error)
    await vi.waitFor(()=>expect(spawned.length).toBe(1))
    expect(spawned[0].env.HSC_TUTOR_EXPLANATION_BASE_URL).toBe('https://old.example.test/v1')
    await expect(manager.applyTutorExplanationConfig({apiKey:'synthetic-new-key-123',baseUrl:'https://new.example.test/v1',model:'deepseek-test'})).rejects.toThrow('CLOUD_PROVIDER_REFRESH_BUSY')
    await oldRestart
    expect(spawned[0].child.exitCode).not.toBeNull()
    expect(manager.getRuntime().status).not.toBe('ready')
    expect(manager.cloudProviderRevisionApplied(manager.getCloudProviderRevision())).toBe(false)
    vi.mocked(globalThis.fetch).mockImplementation(async (input) => {
      if (String(input).endsWith('/v1/runtime/shutdown')) {
        const child=spawned.at(-1)?.child
        if (child && child.exitCode===null) {child.exitCode=0;child.emit('exit',0,null);child.emit('close',0,null)}
        return new Response('{}',{status:200})
      }
      return new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}',{status:200})
    })
    await manager.restart()
    expect(spawned).toHaveLength(2)
    expect(spawned[1].env.HSC_TUTOR_EXPLANATION_BASE_URL).toBe('https://new.example.test/v1')
    expect(manager.cloudProviderRevisionApplied(manager.getCloudProviderRevision())).toBe(true)
  } finally {
    await manager.stop().catch(()=>undefined)
    spawned.length=0;vi.restoreAllMocks()
    if(previous===undefined) delete process.env.HSC_LOCAL_AGENT_PYTHON; else process.env.HSC_LOCAL_AGENT_PYTHON=previous
  }
})

it('reports the exact safe recovery action when the tutor recovery marker blocks startup',()=>{
 const manager=new LocalServiceManager();manager.reportProtectedStartupFailure('tutor_recovery_marker')
 expect(manager.getRuntime().error).toContain('tutor-explanation-settings.lock.recovery')
 expect(manager.getRuntime().error).toContain('请保留其他文件')
 expect(manager.getRuntime().status).toBe('error')
})

it.each(['storage','media_key_recovery','rotation_model_unclassified','tutor_recovery_marker'] as const)(
 'persists only the fixed protected-startup reason %s',async reason=>{
  const root=await mkdtemp(join(tmpdir(),'hsc-protected-startup-diagnostic-'))
  try {
   const recorder=new DiagnosticRecorder(root)
   const manager=new LocalServiceManager(event=>{void recorder.record({source:'local_service',...event})})
   manager.reportProtectedStartupFailure(reason)
   await recorder.flush()
   const raw=await readFile(recorder.logPath,'utf8');const rows=raw.trim().split('\n').map(line=>JSON.parse(line))
   const failure=rows.find(row=>row.event==='protected_startup_failed')
   expect(failure?.details).toEqual({protected_startup_reason:reason})
   expect(raw).not.toContain('/tmp/hsc-local-service-test')
   expect(sanitizeDiagnosticValue({protected_startup_reason:'PRIVATE_TEXT',path:'PRIVATE_PATH',message:'PRIVATE_SPEECH'})).toEqual({})
  } finally { await rm(root,{recursive:true,force:true}) }
 })
