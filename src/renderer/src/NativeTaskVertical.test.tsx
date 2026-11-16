import {act, cleanup, render, waitFor} from '@testing-library/react'
import {afterEach, expect, it, vi} from 'vitest'
import {spawn, type ChildProcessWithoutNullStreams} from 'node:child_process'
import {mkdtemp, rm} from 'node:fs/promises'
import {tmpdir} from 'node:os'
import {join} from 'node:path'
import type {ComponentProps} from 'react'
import {NativeRealtimeCapabilityGateway, signNativeRealtimeCapability} from '../../main/native-realtime-capabilities'
import {ownedVoiceRuntimePolicyDesktop} from './test/ownedVoiceRuntimePolicy'
import {SessionWorkspace} from './SessionWorkspace'
import {localApi, resetRuntimeCache} from './api'
import type {QwenRealtimeVoiceInput} from './QwenRealtimeVoiceInput'
import type {QwenNativeCapabilityProposal} from './qwenNativeProtocol'

// Only the recorder and device calibration boundary are synthetic. Native
// source admission, the Main gateway, signed backend routes and SQLite are real.
const harness = vi.hoisted(() => ({props: null as ComponentProps<typeof QwenRealtimeVoiceInput> | null}))
vi.mock('./QwenRealtimeVoiceInput', async original => ({
  ...await original<typeof import('./QwenRealtimeVoiceInput')>(),
  QwenRealtimeVoiceInput: (props: ComponentProps<typeof QwenRealtimeVoiceInput>) => {harness.props = props; return null},
}))
vi.mock('./VoiceCalibrationPanel', async () => {
  const {useEffect} = await import('react')
  return {VoiceCalibrationPanel: ({onMicrophoneBindingReadyChange}: {onMicrophoneBindingReadyChange: (ready: boolean) => void}) => {
    useEffect(() => onMicrophoneBindingReadyChange(true), [onMicrophoneBindingReadyChange]); return null
  }}
})
vi.mock('./DocumentCameraCapture', () => ({DocumentCameraCapture: () => null}))
vi.mock('./ContinuousDocumentMonitor', () => ({ContinuousDocumentMonitor: () => null}))
const net = globalThis.fetch
const integrationEnabled = process.env.HSC_RUN_NATIVE_TASK_INTEGRATION === '1'
if (!integrationEnabled) console.info('[SKIPPED] Native task + SQLite integration: run npm run test:integration:native-task')
const token = 'synthetic-native-task-token'
const liveServers = new Set<ChildProcessWithoutNullStreams>()

type Fixture = {process: ChildProcessWithoutNullStreams; url: string; session: string}
async function startFixture(directory: string): Promise<Fixture> {
  const python = process.env.HSC_TEST_PYTHON ?? join(process.cwd(), process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python')
  const server = spawn(python, ['services/local_agent/tests/native_task_start_loopback_server.py', directory], {
    cwd: process.cwd(), env: {PATH: process.env.PATH ?? '', TMPDIR: tmpdir(), TEMP: tmpdir(),
      ...(process.env.SystemRoot ? {SystemRoot: process.env.SystemRoot} : {}), PYTHONPATH: join(process.cwd(), 'services/local_agent/src')},
    stdio: ['pipe', 'pipe', 'pipe'],
  })
  liveServers.add(server)
  const killOnParentExit = (): void => {server.kill('SIGTERM')}
  process.once('exit', killOnParentExit)
  server.once('exit', () => {process.off('exit', killOnParentExit); liveServers.delete(server)})
  server.once('error', () => {if (!server.pid) {process.off('exit', killOnParentExit); liveServers.delete(server)}})
  let errors = ''
  server.stderr.on('data', chunk => {errors += String(chunk).slice(0, 4000)})
  try {
    const info = await new Promise<{port: number; session: string}>((resolve, reject) => {
      let data = ''
      const timer = setTimeout(() => reject(Error('SYNTHETIC_NATIVE_TASK_START_TIMEOUT')), 20_000)
      server.once('error', error => {clearTimeout(timer); reject(error)})
      server.once('exit', code => {clearTimeout(timer); reject(Error(`SYNTHETIC_NATIVE_TASK_EXIT_${code}:${errors.slice(-1000)}`))})
      const onStdout = (chunk: Buffer): void => {
        data += String(chunk)
        const line = data.match(/^(\{[^\n]+\})\r?\n/)
        if (line) {
          clearTimeout(timer); server.stdout.off('data', onStdout); server.stdout.resume()
          try {resolve(JSON.parse(line[1]))} catch (error) {reject(error)}
        }
      }
      server.stdout.on('data', onStdout)
    })
    const url = `http://127.0.0.1:${info.port}`
    await vi.waitFor(async () => expect((await net(`${url}/v1/health`, {headers: {'X-HSC-Session': token}})).ok).toBe(true), {timeout: 10_000})
    return {process: server, url, session: info.session}
  } catch (error) {
    await stopFixture(server)
    throw error
  }
}
async function crashFixture(server: ChildProcessWithoutNullStreams): Promise<void> {
  if (!server.pid || server.exitCode !== null || server.signalCode !== null) throw Error('SYNTHETIC_SERVER_ALREADY_STOPPED')
  const exited = new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => reject(Error('SYNTHETIC_SERVER_CRASH_UNCONFIRMED')), 5000)
    timer.unref()
    server.once('exit', () => {clearTimeout(timer); resolve()})
  })
  if (!server.kill('SIGKILL')) throw Error('SYNTHETIC_SERVER_CRASH_FAILED')
  await exited
}
async function stopFixture(server: ChildProcessWithoutNullStreams): Promise<void> {
  if (!server.pid || server.exitCode !== null || server.signalCode !== null) return
  server.kill('SIGTERM')
  await new Promise<void>((resolve, reject) => {
    let hardTimer: ReturnType<typeof setTimeout> | undefined
    const timer = setTimeout(() => {
      server.kill('SIGKILL')
      hardTimer = setTimeout(() => reject(Error('SYNTHETIC_SERVER_EXIT_UNCONFIRMED')), 3000)
      hardTimer.unref()
    }, 3000)
    timer.unref()
    server.once('exit', () => {clearTimeout(timer); if (hardTimer) clearTimeout(hardTimer); resolve()})
  })
}
async function request(url: string, path: string, method: string, body: unknown, parent?: string): Promise<any> {
  const response = await net(`${url}/v1${path}`, {method, headers: {'X-HSC-Session': token, 'Content-Type':'application/json',
    ...(parent ? {'X-HSC-Parent': parent} : {})}, body: JSON.stringify(body)})
  if (!response.ok) throw Error(`SYNTHETIC_REQUEST_${path}_${response.status}:${await response.text()}`)
  return response.json()
}
afterEach(() => {cleanup(); resetRuntimeCache(); vi.restoreAllMocks(); vi.unstubAllGlobals(); localStorage.clear(); harness.props = null})

it.skipIf(!integrationEnabled)('starts and resumes the same task through signed native routes after an unclean restart', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'hsc-synthetic-native-task-'))
  let fixture: Fixture | undefined
  let gateway: NativeRealtimeCapabilityGateway | undefined
  const event = {sender:{id:71}}
  let host: string | undefined
  try {
    fixture = await startFixture(directory)
    const parent = await request(fixture.url, '/parent/setup', 'POST', {pin:'8264'}) as {parent_token:string}
    await request(fixture.url, '/parent/privacy', 'PUT', {cloud_model_text_allowed:true,cloud_audio_allowed:true}, parent.parent_token)
    const child = 'child-primary-4', sid = fixture.session
    const runtime = {status:'ready' as const,localApiUrl:fixture.url,sessionToken:token,runtimeEpoch:1}
    const owner = { ...runtime, child:{}, revision:0 }
    gateway = new NativeRealtimeCapabilityGateway({allowed:e=>e.sender.id===71,current:()=>owner,
      policy:async()=>({capture:'configured',runtimeEpoch:1}),sign:p=>signNativeRealtimeCapability('cd'.repeat(32),p),fetch:net})
    let activeGateway = gateway
    host = gateway.register(event)
    const operations: string[] = []
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({
      getQwenRealtimeSettings:async()=>({configured:true,enabled:true}),
      getWakeWordSettings:async()=>({childId:child,wakeWord:'小知',enabled:true,inactivitySeconds:60}),
      getVoiceOnboarding:async()=>({childId:child,version:1,step:'completed',updatedAt:new Date().toISOString()}),
      forwardQwenNativeRuntime:async(request:Parameters<NativeRealtimeCapabilityGateway['forward']>[1])=>{
        operations.push(request.operation);return activeGateway.forward(event,request)},
    },()=>({...runtime}),net))
    const initial = await localApi.sessionWorkspace(sid)
    expect(initial.plan_items.find(x=>x.plan_item_id==='synthetic-task')?.status).toBe('pending')
    const view = render(<SessionWorkspace initial={initial} childName="合成孩子" childGrade="四年级" onBack={vi.fn()}/>)
    await waitFor(()=>expect(harness.props?.nativeRuntime).toBeDefined())
    const native = harness.props!.nativeRuntime!
    await native.loadCatalog(host); await native.loadContext(host)
    const voice = await native.admit('开始下一项', {source:'accepted_onset',sessionId:sid,localInputId:'synthetic-task-onset',
      capturedAt:Date.now(),reference:{child_id:child},carryoverConfirmationId:null}, host)
    const proposal: QwenNativeCapabilityProposal = {version:1,host_connection_id:host,voice_input_id:voice.voice_input_id,
      supplier_session_id:'synthetic-supplier',input_item_id:'synthetic-item',response_id:'synthetic-response',
      call_id:'synthetic-call',capability_name:'task.start',args:{}}
    let result!: Awaited<ReturnType<typeof native.propose>>
    await act(async()=>{result=await native.propose(proposal)})
    expect(result.execution.status).toBe('completed')
    expect(operations).toEqual(expect.arrayContaining(['catalog','context','input','proposal']))
    expect((await localApi.sessionWorkspace(sid)).plan_items.find(x=>x.plan_item_id==='synthetic-task')?.status).toBe('active')
    expect((await native.readReceipt(proposal)).execution.status).toBe('completed')
    view.unmount()
    gateway.close(event,host); host=undefined
    // Kill the synthetic backend deliberately. Recovery must preserve the task
    // and pause its timer rather than inventing time while the process was down.
    await crashFixture(fixture.process); fixture = undefined
    fixture = await startFixture(directory)
    expect(fixture.session).toBe(sid)
    runtime.localApiUrl=fixture.url; runtime.runtimeEpoch++
    resetRuntimeCache()
    const recovered = await localApi.sessionWorkspace(sid)
    expect(recovered.plan_items.filter(x=>x.plan_item_id==='synthetic-task')).toHaveLength(1)
    expect(recovered.plan_items.find(x=>x.plan_item_id==='synthetic-task')?.status).toBe('paused')
    const newOwner = {...runtime,child:owner.child,revision:1}
    gateway = new NativeRealtimeCapabilityGateway({allowed:e=>e.sender.id===71,current:()=>newOwner,
      policy:async()=>({capture:'configured',runtimeEpoch:runtime.runtimeEpoch}),sign:p=>signNativeRealtimeCapability('cd'.repeat(32),p),fetch:net})
    activeGateway = gateway
    host = gateway.register(event)
    harness.props = null
    render(<SessionWorkspace initial={recovered} childName="合成孩子" childGrade="四年级" onBack={vi.fn()}/>)
    await waitFor(()=>expect(harness.props?.nativeRuntime).toBeDefined())
    const resumedNative = harness.props!.nativeRuntime!
    await resumedNative.loadCatalog(host); await resumedNative.loadContext(host)
    const resumeVoice = await resumedNative.admit('继续这项任务', {source:'accepted_onset',sessionId:sid,localInputId:'synthetic-resume-onset',
      capturedAt:Date.now(),reference:{child_id:child},carryoverConfirmationId:null}, host)
    const resumeProposal: QwenNativeCapabilityProposal = {version:1,host_connection_id:host,voice_input_id:resumeVoice.voice_input_id,
      supplier_session_id:'synthetic-supplier-2',input_item_id:'synthetic-item-2',response_id:'synthetic-response-2',
      call_id:'synthetic-call-2',capability_name:'task.resume',args:{}}
    let resumed!: Awaited<ReturnType<typeof resumedNative.propose>>
    await act(async()=>{resumed=await resumedNative.propose(resumeProposal)})
    expect(resumed.execution.status).toBe('completed')
    const afterResume = await localApi.sessionWorkspace(sid)
    expect(afterResume.plan_items.filter(x=>x.plan_item_id==='synthetic-task')).toHaveLength(1)
    expect(afterResume.plan_items.find(x=>x.plan_item_id==='synthetic-task')?.status).toBe('active')
  } finally {
    cleanup()
    if (gateway && host) gateway.close(event,host)
    if (fixture) await stopFixture(fixture.process)
    for (const server of liveServers) await stopFixture(server)
    if (liveServers.size) throw Error('SYNTHETIC_SERVER_EXIT_UNCONFIRMED')
    await rm(directory,{recursive:true,force:true})
  }
}, 45_000)
