import { denyUnpricedMoneyEgress } from './money-egress'
import { createMistakeBookPrintHandler } from './mistake-book-print'
import { app, BrowserWindow, crashReporter, dialog, ipcMain, Menu, MenuItem, powerSaveBlocker, session, shell, systemPreferences, safeStorage } from 'electron'
import { existsSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { ParentDraftRecoveryCoordinator, ParentDraftRecoveryStore, createSafeStorageDraftCodec } from './parent-draft-recovery'
import { createParentDraftRecoveryAuthorizer, registerParentDraftRecoveryIpc } from './parent-draft-recovery-ipc'
import { createWindowsDraftAtomicIO } from './parent-draft-windows-atomic'
import { createWindowsDraftCodecPreparation } from './parent-draft-windows-codec'
import { LocalDraftPrivacy } from './local-draft-privacy'
import { extname, join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { access, mkdir, stat, unlink, writeFile } from 'node:fs/promises'
import { LocalServiceManager } from './local-service'
import { RuntimeProviderRefresh, cloudProviderSettingsFingerprint } from './runtime-provider-refresh'
import { PublicReplySpeechService } from './public-reply-speech'
import { executeNativeRealtimeOutput } from './native-realtime-output'
import type { PublicReplySpeechSource, PublicReplySettlement } from '../shared/publicReplySpeech'
import { DataEncryptionKeyStore, ensureDashScopeTextFallback, ModelSettingsStore, resolveModelConfigCredentials, runModelSettingsSmoke, validateModelConfig, type ModelConfig } from './model-settings'
import { collectVideoHardwareInventory } from './hardware-inventory'
import { normalizeCameraPermissionStatus } from './media-access'
import { runtimeWebPreferences } from './window-config'
import { applyWindowsCameraCompatibility } from './camera-capture-compatibility'
import { modelConnectionPayload } from './model-connection'
import { VoiceSettingsStore, summarizeVoiceConfig, validateVoiceConfig, validateWakeWordSettings, type VoiceConfig, type VoiceTrial, type VoiceListeningRating, type WakeWordSettings } from './voice-settings'
import { createVoiceRuntimePolicyHandler } from './voice-runtime-policy'
import { NativeRealtimeCapabilityGateway } from './native-realtime-capabilities'
import { QwenNativeSocketRelay } from './qwen-native-socket'
import { exchangeQwenRealtimeSdp, QwenRealtimeSettingsStore, summarizeQwenRealtime, testQwenRealtimeConfig, validateQwenRealtimeConfig, type QwenRealtimeConfig } from './qwen-realtime'
import { VoiceEvaluationCatalog } from './voice-evaluation'
import { VoiceOnboardingStore, type VoiceOnboarding } from './voice-onboarding'
import { enrollReferenceVoice as enrollQwenReferenceVoice, listReferenceVoiceSamples, QWEN_VOICE_CLONE_MODEL } from './voice-cloning'
import { ReportDeliverySecretStore, type ReportDeliverySecrets } from './report-delivery-secrets'
import { CloudAsrSettingsStore } from './asr-settings'
import { HouseholdProvisioningError, suppressHouseholdProvisioningSlots } from './household-provisioning'
import { createHouseholdSetupDefaultsPort, getHouseholdSetupDefaults, importInstalledHouseholdSetup } from './household-setup-defaults'
import { loadTutorExplanationConfig } from './tutor-explanation-settings'
import { createTutorExplanationHandler, type TutorExplanationCommand } from './tutor-explanation-handler'
import { createTutorExplanationAuthorizer } from './tutor-explanation-authorization'
import { QwenRotationError, qwenRotationRecoveryBinding, recoverQwenKeyRotation, rotateInstalledQwenKey } from './qwen-key-rotation'
import { QwenRotationRecovery } from './qwen-rotation-recovery'
import { PetAudioSettingsStore, createPetAudioSettingsHandler } from './pet-audio-settings'
import { readPetCatalogImport } from './pet-audio-catalog'
import { BoundedRendererRecovery } from './runtime-recovery'
import { ControlledGameProcessController, type ControlledGameLaunch } from './controlled-game'
import { createControlledMediaCloser } from './controlled-media-close'
import { type ControlledCloseReceipt } from './controlled-resource-scope'
import { ControlledMediaOwnership, type MediaOwnershipRecord } from './controlled-media-ownership'
import { ControlledLaunchIntents } from './controlled-launch-intent'
import {NativePrivacyCoordinator} from './native-privacy'
import {captureNativeWriting} from './native-writing'
import type {NativePrivacyMetadata,NativePrivacyCommand,NativePrivacyResponse} from '../shared/nativePrivacy'
import type { ControlledLaunchScope, ControlledLaunchIntent, ControlledLaunchReceipt } from '../shared/controlledLaunch'
import { prepareControlledMediaPlayback } from './controlled-media-playback'
import { createVerifiedQuit } from './verified-quit'
import { createVerifiedQuitRecovery } from './verified-quit-recovery'
import { createRuntimeAwake } from './runtime-awake'
import { resolveFamilyDataPaths } from './data-paths'
import { MediaKeyRecoveryRequiredError } from './protected-media-profile'
import { prepareRuntimeProfile, resolveRuntimeProfile } from './runtime-profile'
import { DiagnosticRecorder, installProcessDiagnosticHandlers, type DiagnosticEvent } from './diagnostics'
import { createParentEntryDelivery, createParentEntryShortcut, parentEntryMenuItem, PARENT_ENTRY_READY } from './parent-entry'
import { PilotTestEmailCoordinator } from './pilot-test-email'
import { importPilotConsent } from './pilot-consent-provision'

// The child has no click-first interaction. Permit the companion's verified
// voice pipeline to start from wake/proactive events, while product-level media
// ownership and privacy gates continue to decide what may play.
app.commandLine.appendSwitch('autoplay-policy', 'no-user-gesture-required')
applyWindowsCameraCompatibility(process.platform, app.commandLine)

const runtimeProfile = resolveRuntimeProfile(app.getPath('userData'), process.env, app.isPackaged, process.argv)
prepareRuntimeProfile(runtimeProfile, process.env)
app.setPath('userData', runtimeProfile.userDataPath)
if (runtimeProfile.mode === 'trial') app.setPath('sessionData', join(runtimeProfile.userDataPath, 'browser-session'))
const isolatedDevelopmentProfile = runtimeProfile.mode === 'formal' && !app.isPackaged && Boolean(process.env.HSC_ELECTRON_USER_DATA_DIR)

const diagnosticRecorder = new DiagnosticRecorder(join(resolveFamilyDataPaths(app.getPath('userData')).dataDir, 'diagnostics'))
app.setPath('crashDumps', diagnosticRecorder.crashDumpPath)
crashReporter.start({ uploadToServer: false, compress: false })
installProcessDiagnosticHandlers(diagnosticRecorder)
void diagnosticRecorder.record({
  source: 'main',
  event: 'app_process_started',
  details: { version: app.getVersion(), platform: process.platform, architecture: process.arch, packaged: app.isPackaged }
})
const localService = new LocalServiceManager((event) => {
  void diagnosticRecorder.record({ source: 'local_service', ...event })
})
const pilotTestEmail = new PilotTestEmailCoordinator({
  enabled: process.env.HSC_PILOT_TEST_EMAIL_ENABLED !== '0',
  recipient: process.env.HSC_PILOT_TEST_RECIPIENT?.trim() || '',
  sendHour: (() => {
    const value = Number(process.env.HSC_PILOT_TEST_SEND_HOUR ?? 22)
    return Number.isInteger(value) && value >= 0 && value <= 23 ? value : 22
  })(),
}, {
  runtime: () => localService.getRuntime(),
  diagnostic: (event, details) => diagnosticRecorder.record({ source: 'main', event, details }),
})
const cloudProviders = new RuntimeProviderRefresh()
async function restartChangedCloudProviders(): Promise<void> {
  const revision = localService.getCloudProviderRevision()
  const external = process.env.HSC_LOCAL_API_URL
  await localService.restart()
  if (process.env.HSC_LOCAL_API_URL !== external) throw new Error('CLOUD_PROVIDER_MUTATION_SCOPE_CHANGED')
  // External-managed restart remains health-only and consumes no desktop cache.
  if (!external && !localService.cloudProviderRevisionApplied(revision)) {
    throw new Error('CLOUD_PROVIDER_MUTATION_NOT_APPLIED')
  }
}
let modelSettings: ModelSettingsStore
let asrSettings: CloudAsrSettingsStore
let voiceSettings: VoiceSettingsStore
let qwenRealtimeSettings: QwenRealtimeSettingsStore
let voiceEvaluation: VoiceEvaluationCatalog
let dataEncryptionKeyStore: DataEncryptionKeyStore
let reportDeliverySecrets: ReportDeliverySecretStore
const allowedMediaWindows = new Set<number>()
let quitAfterServiceStop = false
let primaryWindow: BrowserWindow | null = null
const runtimeAwake = createRuntimeAwake(powerSaveBlocker, () => primaryWindow, () => {
  void diagnosticRecorder.record({ source: 'main', event: 'runtime_awake_unconfirmed', level: 'warn', details: { error_code: 'RUNTIME_AWAKE_UNCONFIRMED' } }).catch(() => {})
})
const parentEntry = createParentEntryDelivery(() => primaryWindow, createWindow)
let controlledMediaWindow: BrowserWindow | null = null
const liveControlledMediaWindows = new Set<BrowserWindow>()
const controlledMediaIds = new WeakMap<BrowserWindow, string>()
const controlledMediaReasons = new WeakMap<BrowserWindow, string>()
let mediaOwnership: ControlledMediaOwnership | undefined
const ownedMedia = (): ControlledMediaOwnership => mediaOwnership ??= new ControlledMediaOwnership(join(app.getPath('userData'), 'native-media-ownership'))
const observedMediaClosures = new Map<string, { record: MediaOwnershipRecord; reason: string; proof: 'observed_closed' | 'cancelled_before_create' }>()
function flushObservedMediaClosures(): void {
  for (const [id, observed] of observedMediaClosures) {
    try {
      const current = ownedMedia().read(id)
      if (!current || current.instanceId !== observed.record.instanceId) throw new Error('MEDIA_OWNER_CONFLICT')
      ownedMedia().closed(current, observed.proof)
      observedMediaClosures.delete(id)
      if (observed.proof === 'observed_closed' && primaryWindow && !primaryWindow.isDestroyed()) primaryWindow.webContents.send('controlled-media:closed', observed.reason, id)
    } catch { /* Keep the physical close proof in memory for an exact retry. */ }
  }
}
let controlledMediaTimer: NodeJS.Timeout | null = null
let controlledMediaCloseReason = 'window_closed'
const controlledGame = new ControlledGameProcessController((reason, gameSessionId) => {
  if (primaryWindow && !primaryWindow.isDestroyed()) {
    primaryWindow.webContents.send('controlled-game:closed', reason, gameSessionId)
  }
}, {durable:{directory:join(app.getPath('userData'),'native-game-ownership'),supervisorPath:join(__dirname,'controlled-game-supervisor.js')}})
let launchIntents:ControlledLaunchIntents|undefined
async function readNativeLaunch(intent:ControlledLaunchIntent,cancel=false):Promise<(Omit<ControlledLaunchReceipt,'activityId'>&{activityId:string|null})|null>{
  const runtime=localService.getRuntime(),url=new URL(runtime.localApiUrl)
  if(!['127.0.0.1','localhost','::1','[::1]'].includes(url.hostname))throw Error('LAUNCH_READBACK_LOCAL_ONLY')
  const response=await fetch(`${url.origin}/v1/sessions/${encodeURIComponent(intent.sessionId)}/controlled-launch-intents/${encodeURIComponent(intent.intentId)}${cancel?'/cancel-unstarted':''}`,
    {method:cancel?'POST':'GET',headers:{'X-HSC-Session':runtime.sessionToken,'Content-Type':'application/json'},signal:AbortSignal.timeout(2500),cache:'no-store',
      ...(cancel?{body:JSON.stringify({kind:intent.kind,request_id:intent.requestId})}:{})})
  if(response.status===404)return null
  if(!response.ok)throw Error('LAUNCH_READBACK_UNAVAILABLE')
  const text=await response.text();if(text.length>4096)throw Error('LAUNCH_READBACK_INVALID')
  const value=JSON.parse(text)
  if(value.intentId!==intent.intentId||value.childId!==intent.childId||value.sessionId!==intent.sessionId||value.kind!==intent.kind||value.requestId!==intent.requestId)throw Error('LAUNCH_READBACK_SCOPE')
  return value
}
const ownedLaunches=():ControlledLaunchIntents=>launchIntents??=new ControlledLaunchIntents(join(app.getPath('userData'),'native-launch-intents'),{
  epoch:async scope=>{
    const runtime=localService.getRuntime(),url=new URL(runtime.localApiUrl)
    if(!['127.0.0.1','localhost','::1','[::1]'].includes(url.hostname))throw Error('LAUNCH_READBACK_LOCAL_ONLY')
    const response=await fetch(`${url.origin}/v1/sessions/${encodeURIComponent(scope.sessionId)}/controlled-launch-epoch`,
      {method:'POST',headers:{'X-HSC-Session':runtime.sessionToken},signal:AbortSignal.timeout(5000),cache:'no-store'})
    if(!response.ok)throw Error('LAUNCH_EPOCH_UNAVAILABLE')
    const value=await response.json()
    if(value.childId!==scope.childId||value.sessionId!==scope.sessionId)throw Error('LAUNCH_EPOCH_OWNER_CHANGED')
    return value.epoch
  },
  read:async intent=>{
    const value=await readNativeLaunch(intent)
    if(value===null||(value.status==='cancelled_before_start'&&value.activityId===null))return null
    if(typeof value.activityId!=='string')throw Error('LAUNCH_READBACK_INVALID')
    return {...value,activityId:value.activityId}
  },
  cancelMissing:async intent=>{
    const value=await readNativeLaunch(intent,true)
    return value?.status==='cancelled_before_start'&&value.activityId===null
  },
  confirmNoDispatch:async(binding,proof,version)=>{
    const runtime=localService.getRuntime(),url=new URL(runtime.localApiUrl)
    if(!['127.0.0.1','localhost','::1','[::1]'].includes(url.hostname))throw Error('LAUNCH_READBACK_LOCAL_ONLY')
    const response=await fetch(`${url.origin}/v1/sessions/${encodeURIComponent(binding.sessionId)}/controlled-launch-intents/${encodeURIComponent(binding.intentId)}/native-not-dispatched`,
      {method:'POST',headers:{'X-HSC-Session':runtime.sessionToken,'Content-Type':'application/json'},signal:AbortSignal.timeout(2500),
        body:JSON.stringify({activity_id:binding.activityId,proof,nonce:version.nonce,native_revision:version.revision,
          signature:localService.signNativeLaunchAbsence(binding,proof,version)})})
    if(!response.ok)throw Error('LAUNCH_CANCELLATION_UNCONFIRMED')
    return await response.json() as ControlledLaunchReceipt
  },
  close:(kind,id)=>kind==='video'?closeControlledMedia('launch_recovery',id):controlledGame.close('launch_recovery',id),
  hasResource:async(kind,id)=>kind==='video'?ownedMedia().read(id)!==null:controlledGame.hasDurableRecord(id),
})
async function nativeParentCall(path:string,body:unknown|undefined,parentToken:string,metadata?:NativePrivacyMetadata):Promise<NativePrivacyResponse>{
  const runtime=localService.getRuntime(),url=new URL(runtime.localApiUrl)
  if(!['127.0.0.1','localhost','::1','[::1]'].includes(url.hostname)||!path.startsWith('/v1/parent/'))throw Error('NATIVE_PRIVACY_LOCAL_ONLY')
  const response=await fetch(url.origin+path,{method:body===undefined?'GET':'POST',
    headers:{'X-HSC-Session':runtime.sessionToken,'X-HSC-Parent':parentToken,'Content-Type':'application/json',
      ...(metadata?{'X-HSC-Native-Privacy':Buffer.from(JSON.stringify(localService.signNativePrivacyPermit(metadata))).toString('base64url')}:{})},
    ...(body===undefined?{}:{body:JSON.stringify(body)}),cache:'no-store',signal:AbortSignal.timeout(120000)})
  const text=await response.text();if(text.length>1024*1024)throw Error('NATIVE_PRIVACY_RESPONSE_INVALID')
  return {status:response.status,body:JSON.parse(text)}
}
async function endNativeResource(binding:ControlledLaunchReceipt):Promise<void>{
  const runtime=localService.getRuntime(),group=binding.kind==='video'?'controlled-content':'controlled-games'
  const key=binding.kind==='video'?'content_session_id':'game_session_id'
  const response=await fetch(`${runtime.localApiUrl}/v1/sessions/${encodeURIComponent(binding.sessionId)}/${group}/end`,
    {method:'POST',headers:{'X-HSC-Session':runtime.sessionToken,'Content-Type':'application/json'},signal:AbortSignal.timeout(10000),
      body:JSON.stringify({[key]:binding.activityId,reason:'privacy_operation',actual_seconds:0})})
  if(!response.ok)throw Error('NATIVE_PRIVACY_SETTLEMENT_UNCONFIRMED')
  const result=await response.json()
  if(result[key]!==binding.activityId||result.child_id!==binding.childId||result.session_id!==binding.sessionId||result.status==='active'||result.task_resumed===true)throw Error('NATIVE_PRIVACY_SETTLEMENT_UNCONFIRMED')
}

function draftRecoveryOwner() {
  if (!primaryWindow || primaryWindow.isDestroyed() || primaryWindow.webContents.isDestroyed()) return null
  return { contents: primaryWindow.webContents, frame: primaryWindow.webContents.mainFrame }
}
function draftRecoveryProfileId(): string {
  const root = resolve(app.getPath('userData')), paths = resolveFamilyDataPaths(root)
  const normalize = (value: string) => process.platform === 'win32' ? resolve(value).toLowerCase() : resolve(value)
  return createHash('sha256').update(JSON.stringify([normalize(root), normalize(paths.dataDir), normalize(paths.databasePath)])).digest('hex')
}
let draftRecovery: ParentDraftRecoveryCoordinator | undefined
let draftCodecDispose: (() => Promise<void>) | undefined
let draftCodecClosing:Promise<void>|null=null
let draftPreparation:ReturnType<typeof createWindowsDraftCodecPreparation>|undefined
let draftPrepareResources:(()=>Promise<void>)|undefined
let draftQuitDraining=false
function resumeDraftPreparation():void{draftQuitDraining=false;draftRecovery?.resume();draftPreparation?.resume()}
let draftShutdownFailed=false
let draftShutdownFailureWindow:BrowserWindow|null=null
function retryDraftShutdown():void{
  if(!draftShutdownFailed||draftCodecClosing)return
  // This state is reached only after the original backend stop succeeded.
  // Retry the same disposer; never restart a service or replace an unconfirmed codec.
  quitAfterServiceStop=true
  app.quit()
}
function showDraftShutdownFailure():void{
  if(!draftShutdownFailed)return
  if(draftShutdownFailureWindow&&!draftShutdownFailureWindow.isDestroyed()){
    if(draftShutdownFailureWindow.isMinimized())draftShutdownFailureWindow.restore()
    draftShutdownFailureWindow.show();draftShutdownFailureWindow.focus();return
  }
  const page='data:text/html;charset=utf-8,'+encodeURIComponent(`<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><title>安全退出尚未确认</title><style>body{font:18px/1.7 system-ui,sans-serif;max-width:36em;margin:3em auto;padding:0 2em;background:#f5f6f8;color:#1b2733}a{display:inline-block;padding:.6em 1em;background:#174ea6;color:white;border-radius:.4em}a:focus{outline:3px solid #111;outline-offset:4px}small{display:block;margin-top:2em}</style><h1>安全退出尚未确认</h1><p>后台已停止，但草稿保护组件尚未确认安全退出。应用已暂停，当前不会启动另一个后台或草稿组件。</p><p>请重试安全退出。如果仍然失败，请重启电脑后重新打开应用。应用不会自动重启电脑。</p><a href="hsc-draft-quit:retry">重试安全退出</a><small>错误码：DRAFT_RECOVERY_IO</small></html>`)
  const window=new BrowserWindow({title:'家庭学习伙伴 · 安全退出尚未确认',width:700,height:540,minWidth:560,minHeight:440,show:false,autoHideMenuBar:true,
    webPreferences:{sandbox:true,contextIsolation:true,nodeIntegration:false,webSecurity:true,partition:'draft-shutdown-failure'}})
  draftShutdownFailureWindow=window
  window.setMenu(null)
  window.webContents.setWindowOpenHandler(()=>({action:'deny'}))
  window.webContents.session.setPermissionRequestHandler((_contents,_permission,callback)=>callback(false))
  window.webContents.session.setPermissionCheckHandler(()=>false)
  window.webContents.session.webRequest.onBeforeRequest({urls:['<all_urls>']},(details,callback)=>callback({cancel:details.url!==page}))
  window.webContents.on('will-navigate',(event,url)=>{event.preventDefault();if(url==='hsc-draft-quit:retry')retryDraftShutdown()})
  window.on('close',event=>{if(!quitAfterServiceStop){event.preventDefault();retryDraftShutdown()}})
  window.on('closed',()=>{if(draftShutdownFailureWindow===window)draftShutdownFailureWindow=null})
  void window.loadURL(page).catch(()=>{void diagnosticRecorder.record({source:'main',event:'draft_codec_dispose_failed',level:'error',details:{error_code:'DRAFT_RECOVERY_IO'}}).catch(()=>{})})
  window.show();window.focus()
}
let acknowledgedAbnormalBackend: (() => boolean) | null = null
let quitDiagnosticsFailed = false
let quitFailureWatch: (() => void) | null = null
const quitFailureRecovery = createVerifiedQuitRecovery({
  canExit: () => quitAfterServiceStop,
  retry: () => {
    if (quitDiagnosticsFailed) {
      if (draftCodecClosing) return
      quitAfterServiceStop = true
    }
    app.quit()
  },
  loadFailed: () => { void diagnosticRecorder.record({ source: 'main', event: 'graceful_shutdown_failed', level: 'error', details: { error_code: 'LOCAL_SERVICE_SHUTDOWN_PENDING' } }).catch(() => {}) },
})
function showServiceQuitFailure(continueAfterNaturalClose = false): void {
  quitFailureWatch?.(); quitFailureWatch = null
  const acknowledge = localService.captureAbnormalShutdownAcknowledgement()
  if (acknowledge) {
    quitFailureRecovery.show('backend_abnormal', () => {
      if (!acknowledge()) return false
      acknowledgedAbnormalBackend = acknowledge
      return true
    })
    return
  }
  quitFailureRecovery.show('backend_pending')
  if (!continueAfterNaturalClose) return
  quitFailureWatch = localService.observeShutdownClosure(receipt => {
    // A subsequent tick releases createVerifiedQuit's original pending promise.
    // The same original closure is checked again before continuing its quit.
    if (receipt.graceful) setImmediate(() => {
      if (draftQuitDraining && receipt.isCurrent()) app.quit()
    })
    else if (receipt.isCurrent()) showServiceQuitFailure()
  })
}
let draftPrivacy: LocalDraftPrivacy | undefined
function ownedDraftPrivacy(): LocalDraftPrivacy {
  return draftPrivacy ??= new LocalDraftPrivacy(app.getPath('userData'), {
    revokeChild: child => ownedDrafts().revokeChild(child), revokeAll: () => ownedDrafts().revokeAll(),
    eraseChild: child => ownedDrafts().eraseChild(child), eraseAll: () => ownedDrafts().eraseAll()
  },undefined,async()=>{ownedDrafts();await draftPrepareResources?.()})
}
function ownedDrafts(): ParentDraftRecoveryCoordinator {
  if (draftRecovery) return draftRecovery
  const userDataPath = app.getPath('userData')
  const profileId = draftRecoveryProfileId()
  const legacyCodec = createSafeStorageDraftCodec(safeStorage)
  if(process.platform==='win32'){
    const preparation=createWindowsDraftCodecPreparation({userDataPath,helperDirectory:join(userDataPath,'parent-draft-codec-helper'),profileId,legacyCodec})
    draftPreparation=preparation;draftCodecDispose=()=>preparation.dispose()
    let readyStore:ParentDraftRecoveryStore|undefined
    const currentStore=():ParentDraftRecoveryStore=>{if(!readyStore)throw Error('DRAFT_RECOVERY_IO');return readyStore}
    const prepare=async()=>{
      if(draftQuitDraining)throw Error('DRAFT_RECOVERY_REVOKED')
      const codec=await preparation.prepare()
      if(draftQuitDraining||draftRecoveryProfileId()!==profileId||app.getPath('userData')!==userDataPath)throw Error('DRAFT_RECOVERY_REVOKED')
      if(!readyStore){const atomicIO=createWindowsDraftAtomicIO(join(userDataPath,'parent-draft-recovery'),join(userDataPath,'parent-draft-commit-helper'),codec.commitPort)
        readyStore=new ParentDraftRecoveryStore({userDataPath,profileId,codec,atomicIO})}
    }
    draftPrepareResources=prepare
    const store={profileId,read:(...args:Parameters<ParentDraftRecoveryStore['read']>)=>currentStore().read(...args),
      recover:(...args:Parameters<ParentDraftRecoveryStore['recover']>)=>currentStore().recover(...args),
      write:(...args:Parameters<ParentDraftRecoveryStore['write']>)=>currentStore().write(...args),
      eraseChild:(child:string)=>currentStore().eraseChild(child),eraseAll:()=>currentStore().eraseAll()}
    draftRecovery=new ParentDraftRecoveryCoordinator({store,prepare,
      authorize:createParentDraftRecoveryAuthorizer({owner:draftRecoveryOwner,runtime:()=>localService.getRuntime(),managed:()=>!process.env.HSC_LOCAL_API_URL,profileId:draftRecoveryProfileId,fetch}),
      isPrivacyBlocked:child=>Boolean(ownedLaunches().activePrivacy())||ownedDraftPrivacy().isPrivacyBlocked(child)})
  }else{
    draftRecovery=new ParentDraftRecoveryCoordinator({store:new ParentDraftRecoveryStore({userDataPath,profileId,codec:legacyCodec}),
      authorize:createParentDraftRecoveryAuthorizer({owner:draftRecoveryOwner,runtime:()=>localService.getRuntime(),managed:()=>!process.env.HSC_LOCAL_API_URL,profileId:draftRecoveryProfileId,fetch}),
      isPrivacyBlocked:child=>Boolean(ownedLaunches().activePrivacy())||ownedDraftPrivacy().isPrivacyBlocked(child)})
  }
  if(draftQuitDraining)draftRecovery.quiesce()
  return draftRecovery
}

let privacyCoordinator:NativePrivacyCoordinator|undefined
const ownedPrivacy=():NativePrivacyCoordinator=>privacyCoordinator??=new NativePrivacyCoordinator(ownedLaunches(),{
  call:nativeParentCall,
  permit:async(metadata,token,fence)=>{
    const result=await nativeParentCall('/v1/parent/native-privacy-permits/'+(fence?'fence':'read'),localService.signNativePrivacyPermit(metadata),token)
    if(result.status!==200)throw Error('NATIVE_PRIVACY_RECEIPT_UNAVAILABLE')
    const value=result.body as NativePrivacyMetadata&{status:string}
    if(Object.keys(metadata).some(k=>metadata[k as keyof NativePrivacyMetadata]!==value[k as keyof NativePrivacyMetadata]))throw Error('NATIVE_PRIVACY_SCOPE_CHANGED')
    return value
  },
  end:endNativeResource,
  closeLegacy:async(childId,token)=>{
    type Resource={kind:'video'|'game';activity_id:string;child_id:string;session_id:string;status:string}
    const active=await nativeParentCall('/v1/parent/native-controlled-resources'+(childId?'?child_id='+encodeURIComponent(childId):''),undefined,token)
    if(active.status!==200||!Array.isArray((active.body as {items?:unknown}).items))throw Error('NATIVE_PRIVACY_RESOURCE_SCOPE_UNKNOWN')
    const rows=(active.body as {items:Resource[]}).items
    for(const kind of ['video','game'] as const){
      const ids=new Set([...(kind==='video'?ownedMedia().pendingIds():controlledGame.pendingIds()),...rows.filter(row=>row.kind===kind).map(row=>row.activity_id)])
      for(const id of ids){
        const scoped=await nativeParentCall(`/v1/parent/native-controlled-resources/${kind}/${encodeURIComponent(id)}`,undefined,token)
        const binding=scoped.status===200?scoped.body as Resource:null
        if(scoped.status!==200&&scoped.status!==404)throw Error('NATIVE_PRIVACY_RESOURCE_SCOPE_UNKNOWN')
        if(childId&&(!binding||binding.child_id!==childId)){
          if(binding)continue
          throw Error('NATIVE_PRIVACY_RESOURCE_SCOPE_UNKNOWN')
        }
        const closed=kind==='video'?await closeControlledMedia('privacy_operation',id):await controlledGame.close('privacy_operation',id)
        if(!closed.closed||closed.sessionId!==id)throw Error('NATIVE_PRIVACY_RESOURCE_UNCONFIRMED')
        if(binding)await endNativeResource({childId:binding.child_id,sessionId:binding.session_id,activityId:id,kind,requestId:'legacy',intentId:'legacy',status:binding.status})
      }
    }
  }
}, ownedDraftPrivacy())
async function closeControlledTarget(kind:'video'|'game',reason:string|undefined,id:string):Promise<ControlledCloseReceipt>{
  const actual=kind==='video'?await closeControlledMedia(reason,id):await controlledGame.close(reason,id)
  if(actual.closed)return actual
  try{
    const proof=await ownedLaunches().closeActivity(kind,id)
    if(proof?.closed&&proof.sessionId===id&&['observed_closed','cancelled_before_dispatch','owner_exit_before_registration'].includes(proof.proof??'')){
      return {closed:true,sessionId:id,proof:proof.proof as ControlledCloseReceipt['proof']}
    }
    return actual
  }catch{return actual}
}
const autoLaunchMarker = (): string => join(app.getPath('userData'), 'auto-launch-initialized')
const rendererRecovery = new BoundedRendererRecovery()

function validateReportDeliverySecrets(values: ReportDeliverySecrets): ReportDeliverySecrets {
  if (!values || typeof values !== 'object') throw new Error('送达通道凭据格式无效。')
  const smtpPassword = typeof values.smtpPassword === 'string' ? values.smtpPassword : undefined
  const wecomWebhook = typeof values.wecomWebhook === 'string' ? values.wecomWebhook : undefined
  if (smtpPassword !== undefined && (smtpPassword.length < 1 || smtpPassword.length > 1000)) throw new Error('邮件应用专用密码格式无效。')
  if (wecomWebhook !== undefined && (!wecomWebhook.startsWith('https://') || wecomWebhook.length > 2000)) throw new Error('企业微信 Webhook 格式无效。')
  return { ...(smtpPassword ? { smtpPassword } : {}), ...(wecomWebhook ? { wecomWebhook } : {}) }
}

async function openControlledMedia(values: { path: string; title: string; maxSeconds: number; contentSessionId?: string }): Promise<{ opened: true }> {
  if (!values || typeof values.path !== 'string' || typeof values.title !== 'string') throw new Error('受控内容参数无效。')
  const contentSessionId = values.contentSessionId
  if (typeof contentSessionId !== 'string' || !contentSessionId.trim() || contentSessionId.length > 128) throw new Error('视频记录缺少原活动身份，没有打开新窗口。')
  if (ownedMedia().read(contentSessionId) || [...liveControlledMediaWindows].some(window => controlledMediaIds.get(window) === contentSessionId)) throw new Error('原视频已打开或关闭，没有重复打开。')
  const localPath = resolve(values.path)
  if (!['.mp4', '.m4v', '.mov', '.webm'].includes(extname(localPath).toLowerCase())) throw new Error('不支持这种本地视频格式。')
  const file = await stat(localPath)
  if (!file.isFile()) throw new Error('受控内容文件不可用。')
  if (!Number.isFinite(values.maxSeconds)) throw new Error('视频时限无效。')
  const maxSeconds = Math.min(3600, Math.max(30, Math.floor(values.maxSeconds)))
  if (ownedMedia().read(contentSessionId) || [...liveControlledMediaWindows].some(window => controlledMediaIds.get(window) === contentSessionId)) throw new Error('原视频状态已变化，没有重复打开。')

  if (!(await closeControlledMedia('replaced')).closed) throw new Error('原视频尚未确认关闭，没有打开新窗口。')
  let ownershipRecord = await ownedMedia().reserve(contentSessionId)
  if (controlledMediaTimer) clearTimeout(controlledMediaTimer)
  controlledMediaTimer = null
  controlledMediaCloseReason = 'window_closed'

  let mediaWindow: BrowserWindow
  try { mediaWindow = new BrowserWindow({
    width: 1100,
    height: 720,
    minWidth: 720,
    minHeight: 480,
    parent: primaryWindow ?? undefined,
    autoHideMenuBar: true,
    title: values.title.slice(0, 120),
    backgroundColor: '#111111',
    webPreferences: runtimeWebPreferences,
  }) } catch (error) {
    observedMediaClosures.set(contentSessionId, { record: ownershipRecord, reason: 'launch_failed', proof: 'cancelled_before_create' })
    flushObservedMediaClosures()
    throw error
  }
  controlledMediaWindow = mediaWindow
  liveControlledMediaWindows.add(mediaWindow)
  controlledMediaIds.set(mediaWindow, contentSessionId)
  const windowId = mediaWindow.webContents.id
  mediaWindow.on('closed', () => {
    liveControlledMediaWindows.delete(mediaWindow)
    allowedMediaWindows.delete(windowId)
    observedMediaClosures.set(contentSessionId, { record: ownershipRecord, reason: controlledMediaReasons.get(mediaWindow) ?? 'window_closed', proof: 'observed_closed' })
    flushObservedMediaClosures()
    if (controlledMediaWindow !== mediaWindow) return
    if (controlledMediaTimer) clearTimeout(controlledMediaTimer)
    controlledMediaTimer = null
    controlledMediaWindow = null
    controlledMediaCloseReason = 'window_closed'
  })
  try { ownershipRecord = ownedMedia().opened(ownershipRecord) }
  catch (error) { await closeControlledMedia('playback_failed', contentSessionId); throw error }
  allowedMediaWindows.add(windowId)
  mediaWindow.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  mediaWindow.webContents.on('console-message', (event) => {
    if (event.message === 'HSC_CONTROLLED_MEDIA_ENDED' && controlledMediaWindow === mediaWindow) {
      controlledMediaCloseReason = 'media_ended'
      controlledMediaReasons.set(mediaWindow, 'media_ended')
      mediaWindow.close()
    }
  })
  mediaWindow.webContents.on('did-fail-load', () => {
    if (controlledMediaWindow === mediaWindow) {
      controlledMediaCloseReason = 'playback_failed'
      controlledMediaReasons.set(mediaWindow, 'playback_failed')
      mediaWindow.close()
    }
  })
  controlledMediaTimer = setTimeout(() => {
    if (controlledMediaWindow === mediaWindow) {
      controlledMediaCloseReason = 'time_limit'
      controlledMediaReasons.set(mediaWindow, 'time_limit')
      mediaWindow.close()
    }
  }, maxSeconds * 1000)
  try {
    await prepareControlledMediaPlayback(mediaWindow, pathToFileURL(localPath).toString(), () => controlledMediaWindow === mediaWindow)
  } catch (error) {
    if (controlledMediaWindow === mediaWindow) await closeControlledMedia('playback_failed', contentSessionId)
    throw error
  }
  return { opened: true }
}

async function closeControlledMedia(reason = 'child_requested', expectedSessionId?: string): Promise<ControlledCloseReceipt> {
  flushObservedMediaClosures()
  if (expectedSessionId !== undefined) {
    const target = [...liveControlledMediaWindows].find(window => controlledMediaIds.get(window) === expectedSessionId)
    if (target) await createControlledMediaCloser(() => target, (window, value) => controlledMediaReasons.set(window, value.slice(0, 120)), 2000, () => !target.isDestroyed())(reason)
    flushObservedMediaClosures()
    try {
      const receipt = await ownedMedia().receipt(expectedSessionId)
      if (!receipt.closed && !target && liveControlledMediaWindows.size) return { closed: false, sessionId: expectedSessionId, proof: 'identity_mismatch' }
      return receipt
    } catch { return { closed: false, sessionId: expectedSessionId, proof: 'unknown' } }
  }
  // Only main-owned replacement/shutdown closes all retained windows unscoped.
  const receipts = await Promise.all([...liveControlledMediaWindows].map(target => createControlledMediaCloser(
    () => target, (window, value) => controlledMediaReasons.set(window, value.slice(0, 120)),
    2000, () => !target.isDestroyed(),
  )(reason)))
  flushObservedMediaClosures()
  try { return { closed: receipts.every(receipt => receipt.closed) && liveControlledMediaWindows.size === 0 && observedMediaClosures.size === 0 && (await ownedMedia().recover()).unresolved === 0 } }
  catch { return { closed: false, proof: 'unknown' } }
}

function createWindow(): void {
  if(draftQuitDraining)return
  if(draftShutdownFailed){showDraftShutdownFailure();return}
  parentEntry.loading()
  const mainWindow = new BrowserWindow({
    title: runtimeProfile.mode === 'trial' ? '家庭学习伙伴 · 隔离试用' : '家庭学习伙伴',
    width: 1280,
    height: 800,
    minWidth: 1024,
    minHeight: 680,
    show: false,
    autoHideMenuBar: false,
    backgroundColor: '#f5f6f8',
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      ...runtimeWebPreferences,
    }
  })
  primaryWindow = mainWindow
  runtimeAwake.watchWindow(mainWindow)
  const parentShortcut = createParentEntryShortcut(parentEntry.request, process.platform)
  mainWindow.webContents.on('before-input-event', (event, input) => {
    if (primaryWindow === mainWindow && mainWindow.isFocused()) parentShortcut.handle(event, input)
  })
  mainWindow.on('blur', () => parentShortcut.reset())
  mainWindow.webContents.on('did-start-loading', () => parentEntry.loading())
  mainWindow.webContents.on('did-start-navigation', (_event, _url, _inPlace, isMainFrame) => {
    if (isMainFrame) draftRecovery?.revokeSender(mainWindow.webContents.id)
  })
  const draftSenderId = mainWindow.webContents.id
  mainWindow.webContents.on('render-process-gone', () => draftRecovery?.revokeSender(draftSenderId))
  mainWindow.webContents.on('destroyed', () => draftRecovery?.revokeSender(draftSenderId))
  mainWindow.webContents.on('will-prevent-unload', () => {
    void diagnosticRecorder.record({ source: 'main', event: 'app_quit_unload_prevented' })
    if(draftQuitDraining&&quitAfterServiceStop&&!draftCodecClosing){
      quitAfterServiceStop=false;resumeDraftPreparation()
      void localService.start().catch(()=>{void diagnosticRecorder.record({source:'main',event:'graceful_shutdown_failed',level:'error'}).catch(()=>{})})
    }
  })
  void diagnosticRecorder.record({ source: 'main', event: 'primary_window_created', details: { webContentsId: mainWindow.webContents.id } })
  let rendererRecoveryTimer: ReturnType<typeof setTimeout> | null = null
  let rendererStableTimer: ReturnType<typeof setTimeout> | null = null
  const recoverRenderer = (reason: string): void => {
    if (rendererRecoveryTimer || mainWindow.isDestroyed()) return
    const delay = rendererRecovery.nextDelay()
    if (delay === null) {
      console.error(`[renderer-recovery] recovery limit reached: ${reason}`)
      void diagnosticRecorder.record({ source: 'renderer', event: 'recovery_exhausted', level: 'error', message: reason })
      setTimeout(() => {
        app.relaunch()
        app.exit(1)
      }, 1_000)
      return
    }
    console.error(`[renderer-recovery] ${reason}; retrying in ${delay}ms`)
    void diagnosticRecorder.record({ source: 'renderer', event: 'recovery_scheduled', level: 'warn', message: reason, details: { delayMs: delay } })
    rendererRecoveryTimer = setTimeout(() => {
      rendererRecoveryTimer = null
      if (!mainWindow.isDestroyed()) mainWindow.reload()
    }, delay)
  }
  const mediaWindowId = mainWindow.webContents.id
  allowedMediaWindows.add(mediaWindowId)
  mainWindow.on('closed', () => {
    void diagnosticRecorder.record({ source: 'main', event: 'primary_window_closed' })
    if (rendererRecoveryTimer) clearTimeout(rendererRecoveryTimer)
    rendererRecoveryTimer = null
    if (rendererStableTimer) clearTimeout(rendererStableTimer)
    rendererStableTimer = null
    allowedMediaWindows.delete(mediaWindowId)
    if (primaryWindow === mainWindow) primaryWindow = null
  })

  mainWindow.on('ready-to-show', () => {
    void diagnosticRecorder.record({ source: 'main', event: 'primary_window_ready_to_show' })
    mainWindow.show()
  })
  mainWindow.on('unresponsive', () => {
    void diagnosticRecorder.record({ source: 'renderer', event: 'window_unresponsive', level: 'error' })
  })
  mainWindow.on('responsive', () => {
    void diagnosticRecorder.record({ source: 'renderer', event: 'window_responsive_after_stall', level: 'warn' })
  })
  mainWindow.webContents.on('did-finish-load', () => {
    void diagnosticRecorder.record({ source: 'renderer', event: 'page_loaded' })
    if (rendererStableTimer) clearTimeout(rendererStableTimer)
    rendererStableTimer = setTimeout(() => rendererRecovery.markStable(), 60_000)
    void mainWindow.webContents
      .executeJavaScript('typeof window.desktop')
      .then((type) => console.info(`[desktop-bridge] window.desktop is ${type}`))
    if (!app.isPackaged && process.env.HSC_CAPTURE_PATH) {
      setTimeout(() => {
        const captureAction = process.env.HSC_CAPTURE_ACTION
        const prepare = captureAction === 'parent' || captureAction === 'parent-unlock' || captureAction === 'model-operations' || captureAction === 'voice-settings' || captureAction === 'voice-evaluation' || captureAction === 'session' || captureAction === 'agent-command' || captureAction === 'draft-save' || captureAction === 'draft-restore' || captureAction === 'completion-release' || captureAction === 'ui-evening' || captureAction === 'ocr-tutor-recovery'
          ? mainWindow.webContents.executeJavaScript(captureAction === 'parent-unlock' || captureAction === 'model-operations' || captureAction === 'voice-settings' || captureAction === 'voice-evaluation'
            ? `(async () => {
                Array.from(document.querySelectorAll('button')).find((button) => button.textContent?.includes('家长中心'))?.click();
                await new Promise((resolve) => setTimeout(resolve, 350));
                const input = document.querySelector('input[aria-label="家长 PIN"]');
                const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                setter.call(input, '2468');
                input.dispatchEvent(new Event('input', { bubbles: true }));
                await new Promise((resolve) => setTimeout(resolve, 100));
                Array.from(document.querySelectorAll('button')).find((button) => button.textContent?.includes('设置并进入') || button.textContent?.includes('解锁'))?.click();
                await new Promise((resolve) => setTimeout(resolve, 1200));
                ${captureAction === 'model-operations' ? "document.querySelector('.model-operations')?.scrollIntoView({ block: 'center' }); await new Promise((resolve) => setTimeout(resolve, 500));" : captureAction === 'voice-settings' || captureAction === 'voice-evaluation' ? "document.querySelector('.voice-settings-panel')?.scrollIntoView({ block: 'center' }); await new Promise((resolve) => setTimeout(resolve, 500));" : ''}
                ${captureAction === 'voice-evaluation' ? "Array.from(document.querySelectorAll('button')).find((button) => button.textContent?.includes('播放官方试听'))?.click(); await new Promise((resolve) => setTimeout(resolve, 900));" : ''}
              })()`
              : captureAction === 'session' || captureAction === 'agent-command'
              ? `(async () => {
                  Array.from(document.querySelectorAll('button')).find((button) => button.textContent?.includes('开始学习') || button.textContent?.includes('继续学习'))?.click();
                  await new Promise((resolve) => setTimeout(resolve, 1400));
                  ${captureAction === 'agent-command' ? `
                    const input = document.querySelector('input[aria-label="对学习伙伴说"]');
                    if (!(input instanceof HTMLInputElement)) throw new Error('agent command input was not rendered');
                    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                    setter.call(input, '我今天有数学作业。');
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    await new Promise((resolve) => setTimeout(resolve, 100));
                    Array.from(document.querySelectorAll('button')).find((button) => button.textContent?.trim() === '发送')?.click();
                    await new Promise((resolve) => setTimeout(resolve, 1800));` : ''}
                })()`
              : captureAction === 'draft-save' || captureAction === 'draft-restore'
                ? `(async () => {
                    Array.from(document.querySelectorAll('button')).find((button) => !button.disabled && (button.textContent?.includes('开始学习') || button.textContent?.includes('继续学习')))?.click();
                    await new Promise((resolve) => setTimeout(resolve, 1000));
                    const input = Array.from(document.querySelectorAll('input')).find((element) => element.getAttribute('placeholder')?.includes('晚饭、洗澡、训练'));
                    if (!input) throw new Error('draft capture could not find the check-in field');
                    if (${captureAction === 'draft-save' ? 'true' : 'false'}) {
                      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                      setter.call(input, '草稿恢复验证：足球训练');
                      input.dispatchEvent(new Event('input', { bubbles: true }));
                      await new Promise((resolve) => setTimeout(resolve, 1400));
                    } else if (input.value !== '草稿恢复验证：足球训练') {
                      throw new Error('draft was not restored after app restart');
                    }
                    input.scrollIntoView({ block: 'center' });
                  })()`
              : captureAction === 'ocr-tutor-recovery'
                ? `(async () => {
                    // Development-only visual acceptance: this creates a
                    // public, synthetic arithmetic image and imports it through
                    // the same UI a child uses. It never opens a camera or sends
                    // family media to a cloud provider.
                    if (sessionStorage.getItem('hsc-ocr-tutor-capture') === 'awaiting-recovery') {
                      await new Promise((resolve) => setTimeout(resolve, 900));
                      if (!document.querySelector('[role="alert"]')) throw new Error('tutor recovery draft was not restored');
                      sessionStorage.setItem('hsc-ocr-tutor-capture', 'done');
                      return;
                    }
                    if (sessionStorage.getItem('hsc-ocr-tutor-capture') === 'done') return;
                    if (sessionStorage.getItem('hsc-ocr-tutor-capture') !== 'prepared') {
                    const runtime = await window.desktop.getRuntimeConfig();
                    const headers = { 'Content-Type': 'application/json', 'X-HSC-Session': runtime.sessionToken };
                    const api = async (path, init = {}) => {
                      const response = await fetch(runtime.localApiUrl + path, { ...init, headers: { ...headers, ...(init.headers || {}) } });
                      if (!response.ok) throw new Error('OCR tutor capture API failed: ' + path + ' (' + response.status + ')');
                      return response.json();
                    };
                    const actionId = () => crypto.randomUUID();
                    const bootstrap = await api('/v1/bootstrap');
                    let sessionId = bootstrap.active_session?.session_id;
                    if (!sessionId) sessionId = (await api('/v1/sessions', { method: 'POST', body: JSON.stringify({ child_id: 'child-primary-4', client_action_id: actionId() }) })).session_id;
                    for (let index = 0; index < 8; index += 1) {
                      const workspace = await api('/v1/sessions/' + sessionId);
                      if (workspace.session.state === 'study') break;
                      if (workspace.session.state === 'check_in') await api('/v1/sessions/' + sessionId + '/transition', { method: 'POST', body: JSON.stringify({ expected_state: 'check_in', target_state: 'classroom_recall', reason: 'synthetic OCR capture', client_action_id: actionId() }) });
                      else if (workspace.session.state === 'classroom_recall') { await api('/v1/sessions/' + sessionId + '/recalls/none', { method: 'POST', body: JSON.stringify({ client_action_id: actionId() }) }); await api('/v1/sessions/' + sessionId + '/transition', { method: 'POST', body: JSON.stringify({ expected_state: 'classroom_recall', target_state: 'homework_capture', reason: 'synthetic OCR capture', client_action_id: actionId() }) }); }
                      else if (workspace.session.state === 'homework_capture') { await api('/v1/sessions/' + sessionId + '/homework', { method: 'POST', body: JSON.stringify({ subject: '数学', title: '公开算术验收题', expected_minutes: 10, client_action_id: actionId() }) }); await api('/v1/sessions/' + sessionId + '/plan/generate', { method: 'POST', body: JSON.stringify({ client_action_id: actionId() }) }); }
                      else if (workspace.session.state === 'plan_review') await api('/v1/sessions/' + sessionId + '/transition', { method: 'POST', body: JSON.stringify({ expected_state: 'plan_review', target_state: 'study', reason: 'synthetic OCR capture', client_action_id: actionId() }) });
                      else throw new Error('OCR tutor capture requires a study session');
                    }
                    sessionStorage.setItem('hsc-ocr-tutor-capture', 'prepared');
                    window.location.reload();
                    return;
                    }
                    let resume = null;
                    for (let index = 0; index < 20 && !resume; index += 1) {
                      resume = Array.from(document.querySelectorAll('button')).find((button) => !button.disabled && (button.textContent?.includes('开始学习') || button.textContent?.includes('继续学习')));
                      if (!resume) await new Promise((resolve) => setTimeout(resolve, 150));
                    }
                    if (!resume) throw new Error('OCR tutor resume button was not rendered');
                    resume.click();
                    await new Promise((resolve) => setTimeout(resolve, 1200));
                    const voiceInput = document.querySelector('input[aria-label="对学习伙伴说"]');
                    if (!(voiceInput instanceof HTMLInputElement)) throw new Error('OCR tutor command input was not rendered');
                    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                    setter.call(voiceInput, '看一下这题'); voiceInput.dispatchEvent(new Event('input', { bubbles: true }));
                    Array.from(document.querySelectorAll('button')).find((button) => button.textContent?.trim() === '发送')?.click();
                    let fileInput = null;
                    for (let index = 0; index < 24 && !fileInput; index += 1) {
                      fileInput = document.querySelector('.document-capture input[type="file"]');
                      if (!fileInput) await new Promise((resolve) => setTimeout(resolve, 250));
                    }
                    if (!(fileInput instanceof HTMLInputElement)) throw new Error('OCR tutor capture input was not rendered');
                    const canvas = document.createElement('canvas'); canvas.width = 1600; canvas.height = 480;
                    const context = canvas.getContext('2d'); context.fillStyle = '#fff'; context.fillRect(0, 0, 1600, 480); context.fillStyle = '#111'; context.font = 'bold 150px Arial'; context.fillText('12 + 8 = ?', 180, 290);
                    const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/png'));
                    if (!blob) throw new Error('could not create synthetic OCR image');
                    const files = new DataTransfer(); files.items.add(new File([blob], 'public-arithmetic.png', { type: 'image/png' }));
                    Object.defineProperty(fileInput, 'files', { value: files.files }); fileInput.dispatchEvent(new Event('change', { bubbles: true }));
                    await new Promise((resolve) => setTimeout(resolve, 250));
                    Array.from(document.querySelectorAll('button')).find((button) => button.textContent?.includes('确认并识别'))?.click();
                    for (let index = 0; index < 40 && !Array.from(document.querySelectorAll('button')).some((button) => button.textContent?.includes('我已核对题干')); index += 1) await new Promise((resolve) => setTimeout(resolve, 250));
                    const confirm = Array.from(document.querySelectorAll('button')).find((button) => button.textContent?.includes('我已核对题干'));
                    if (!confirm) throw new Error('OCR result did not reach the human confirmation gate');
                    confirm.click(); await new Promise((resolve) => setTimeout(resolve, 500));
                    Array.from(document.querySelectorAll('button')).find((button) => button.textContent?.trim() === '提问')?.click();
                    // With no configured model this intentionally lands in the
                    // visible retry state; it must not fabricate a tutor reply.
                    await new Promise((resolve) => setTimeout(resolve, 900));
                    sessionStorage.setItem('hsc-ocr-tutor-capture', 'awaiting-recovery');
                    window.location.reload();
                  })()`
              : captureAction === 'ui-evening'
                ? `(async () => {
                    // Development-only renderer acceptance. Every transition below
                    // is made through the rendered child/parent controls; it never
                    // opens a camera, writes media, or calls a private API route.
                    const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
                    const findButton = (text) => Array.from(document.querySelectorAll('button')).find((button) => !button.disabled && button.textContent?.trim().includes(text));
                    const findExactButton = (text) => Array.from(document.querySelectorAll('button')).find((button) => !button.disabled && button.textContent?.trim() === text);
                    const click = async (text) => {
                      for (let index = 0; index < 40; index += 1) {
                        const button = findButton(text);
                        if (button) { button.click(); return; }
                        await wait(150);
                      }
                      throw new Error('ui-evening could not find enabled button: ' + text);
                    };
                    const clickExact = async (text) => {
                      for (let index = 0; index < 40; index += 1) {
                        const button = findExactButton(text);
                        if (button) { button.click(); return; }
                        await wait(150);
                      }
                      throw new Error('ui-evening could not find enabled exact button: ' + text);
                    };
                    const setValue = (element, value) => {
                      const prototype = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                      const setter = Object.getOwnPropertyDescriptor(prototype, 'value').set;
                      setter.call(element, value);
                      element.dispatchEvent(new Event('input', { bubbles: true }));
                    };
                    await click('开始学习'); await wait(600);
                    await click('我准备好了'); await wait(450);
                    const recall = document.querySelector('textarea[placeholder*="不用说得很完整"]');
                    if (!(recall instanceof HTMLTextAreaElement)) throw new Error('ui-evening recall input missing');
                    setValue(recall, '我理解了分数比较'); await click('记下来'); await wait(450); await click('接着看作业'); await wait(450);
                    const homework = document.querySelector('input[placeholder*="练习册第32页"]');
                    if (!(homework instanceof HTMLInputElement)) throw new Error('ui-evening homework input missing');
                    setValue(homework, '分数比较练习'); await click('加入作业'); await wait(450); await click('制定今天的计划'); await wait(450);
                    await click('按这个计划开始'); await wait(450); await click('开始这项'); await wait(350); await click('完成这项'); await wait(250); await clickExact('已完成'); await wait(250); await click('确认记录'); await wait(500);
                    await click('回顾一下今天'); await wait(400);
                    const reflection = document.querySelectorAll('.reflection-fields textarea');
                    const nextAction = document.querySelector('.reflection-fields input');
                    if (!(reflection[0] instanceof HTMLTextAreaElement) || !(reflection[1] instanceof HTMLTextAreaElement) || !(nextAction instanceof HTMLInputElement)) throw new Error('ui-evening reflection fields missing');
                    setValue(reflection[0], '会比较分数大小'); setValue(reflection[1], '还要再核对通分步骤'); setValue(nextAction, '明天先做一道通分题');
                    await click('保存复盘并结束'); await wait(900);
                    if (!document.body.textContent?.includes('家长日报已经在本机生成并保存')) throw new Error('ui-evening did not persist the daily report');
                    const home = document.querySelector('button[title="返回首页"]');
                    if (!(home instanceof HTMLButtonElement)) throw new Error('ui-evening home control missing');
                    home.click(); await wait(700); await click('家长中心'); await wait(300);
                    const pin = document.querySelector('input[aria-label="家长 PIN"]');
                    if (!(pin instanceof HTMLInputElement)) throw new Error('ui-evening parent PIN input missing');
                    setValue(pin, '2468'); await click('设置并进入'); await wait(900);
                    if (!document.body.textContent?.includes('家长中心')) throw new Error('ui-evening parent center did not render');
                    if (document.body.textContent?.includes('未确认的本地可观察候选')) throw new Error('ui-evening fabricated a behavior candidate without a camera');
                  })()`
              : captureAction === 'completion-release'
                  ? `(async () => {
                      Array.from(document.querySelectorAll('button')).find((button) => button.textContent?.includes('开始学习') || button.textContent?.includes('继续学习'))?.click();
                      await new Promise((resolve) => setTimeout(resolve, 900));
                      const finish = Array.from(document.querySelectorAll('button')).find((button) => button.textContent?.includes('跳过，直接结束'));
                      if (finish) {
                        finish.click();
                        await new Promise((resolve) => setTimeout(resolve, 1800));
                        return;
                      }
                      const runtime = await window.desktop.getRuntimeConfig();
                      const headers = { 'Content-Type': 'application/json', 'X-HSC-Session': runtime.sessionToken };
                      const api = async (path, init = {}) => {
                        const response = await fetch(runtime.localApiUrl + path, { ...init, headers: { ...headers, ...(init.headers || {}) } });
                        if (!response.ok) throw new Error('completion capture API failed: ' + path + ' (' + response.status + ')');
                        return response.json();
                      };
                      const bootstrap = await api('/v1/bootstrap');
                      const sessionId = bootstrap.active_session?.session_id;
                      if (!sessionId) throw new Error('completion capture requires an active session');
                      for (let index = 0; index < 12; index += 1) {
                        const workspace = await api('/v1/sessions/' + sessionId);
                        const actionId = () => crypto.randomUUID();
                        if (workspace.session.state === 'reflection') break;
                        if (workspace.session.state === 'check_in') {
                          await api('/v1/sessions/' + sessionId + '/planning-context', { method: 'PUT', body: JSON.stringify({ energy_level: 'medium', fixed_commitment_minutes: 0, fixed_commitment_note: null, tomorrow_event_subject: null, tomorrow_event_title: null, tomorrow_event_minutes: 0, client_action_id: actionId() }) });
                          await api('/v1/sessions/' + sessionId + '/transition', { method: 'POST', body: JSON.stringify({ expected_state: 'check_in', target_state: 'classroom_recall', reason: 'capture flow', client_action_id: actionId() }) });
                        } else if (workspace.session.state === 'classroom_recall') {
                          if (!workspace.recalls.length && !workspace.session.recall_declared_empty) await api('/v1/sessions/' + sessionId + '/recalls/none', { method: 'POST', body: JSON.stringify({ client_action_id: actionId() }) });
                          await api('/v1/sessions/' + sessionId + '/transition', { method: 'POST', body: JSON.stringify({ expected_state: 'classroom_recall', target_state: 'homework_capture', reason: 'capture flow', client_action_id: actionId() }) });
                        } else if (workspace.session.state === 'homework_capture') {
                          if (!workspace.homework.length && !workspace.session.homework_declared_empty) await api('/v1/sessions/' + sessionId + '/homework/none', { method: 'POST', body: JSON.stringify({ client_action_id: actionId() }) });
                          await api('/v1/sessions/' + sessionId + '/plan/generate', { method: 'POST', body: JSON.stringify({ client_action_id: actionId() }) });
                        } else if (workspace.session.state === 'plan_review') {
                          await api('/v1/sessions/' + sessionId + '/transition', { method: 'POST', body: JSON.stringify({ expected_state: 'plan_review', target_state: 'study', reason: 'capture flow', client_action_id: actionId() }) });
                        } else if (workspace.session.state === 'break') {
                          await api('/v1/sessions/' + sessionId + '/resume', { method: 'POST', body: JSON.stringify({ client_action_id: actionId() }) });
                        } else if (workspace.session.state === 'study') {
                          for (const item of workspace.plan_items.filter((candidate) => candidate.status === 'pending')) {
                            if (item.source_type === 'planned_break') {
                              await api('/v1/sessions/' + sessionId + '/break', { method: 'POST', body: JSON.stringify({ client_action_id: actionId() }) });
                              await api('/v1/sessions/' + sessionId + '/resume', { method: 'POST', body: JSON.stringify({ client_action_id: actionId() }) });
                            } else {
                              await api('/v1/plan-items/' + item.plan_item_id + '/start', { method: 'POST', body: JSON.stringify({ client_action_id: actionId() }) });
                              await api('/v1/plan-items/' + item.plan_item_id + '/complete', { method: 'POST', body: JSON.stringify({ actual_minutes: 1, completion_evidence_type: 'child_confirmed', completion_note: null, completion_checklist: [], completion_artifact_id: null, client_action_id: actionId() }) });
                            }
                          }
                          const updated = await api('/v1/sessions/' + sessionId);
                          if (updated.plan_items.every((item) => ['completed', 'rescheduled', 'skipped'].includes(item.status))) await api('/v1/sessions/' + sessionId + '/transition', { method: 'POST', body: JSON.stringify({ expected_state: 'study', target_state: 'reflection', reason: 'capture flow', client_action_id: actionId() }) });
                        }
                      }
                      await api('/v1/sessions/' + sessionId + '/devices/register', { method: 'POST', body: JSON.stringify({ device_type: 'camera', system_id_hash: 'completion-release-capture-0001', capabilities: { source: 'development_capture' } }) });
                      window.location.reload();
                    })()`
              : "Array.from(document.querySelectorAll('button')).find((button) => button.textContent?.includes('家长中心'))?.click()")
          : Promise.resolve()
        void prepare
          .then(() => new Promise((resolve) => setTimeout(resolve, 500)))
          .then(() => mainWindow.webContents.capturePage())
          .then((image) => writeFile(process.env.HSC_CAPTURE_PATH!, image.toPNG()))
          .catch((error) => console.error(`[capture-page] ${String(error)}`))
      }, 700)
    }
  })
  mainWindow.webContents.on('render-process-gone', (_event, details) => {
    void diagnosticRecorder.record({ source: 'renderer', event: 'process_gone', level: details.reason === 'clean-exit' ? 'info' : 'error', details })
    if (details.reason !== 'clean-exit') recoverRenderer(`renderer exited: ${details.reason}`)
  })
  mainWindow.webContents.on('did-fail-load', (_event, errorCode, errorDescription, url, isMainFrame) => {
    void diagnosticRecorder.record({ source: 'renderer', event: 'page_load_failed', level: errorCode === -3 ? 'info' : 'error', message: errorDescription, details: { errorCode, url, isMainFrame } })
    if (isMainFrame && errorCode !== -3) recoverRenderer(`main page load failed (${errorCode}: ${errorDescription})`)
  })
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    void shell.openExternal(url)
    return { action: 'deny' }
  })

  if (!app.isPackaged && process.env.ELECTRON_RENDERER_URL) {
    void mainWindow.loadURL(process.env.ELECTRON_RENDERER_URL)
  } else {
    void mainWindow.loadFile(join(__dirname, '../renderer/index.html'))
  }
}

const removeAutoLaunchOnly = process.argv.includes('--remove-auto-launch')
const hardwareInventorySmokeOnly = process.argv.includes('--hardware-inventory-smoke')
const modelSettingsSmokeOnly = process.argv.includes('--model-settings-smoke')
const acquiredLock = removeAutoLaunchOnly || hardwareInventorySmokeOnly || modelSettingsSmokeOnly || isolatedDevelopmentProfile || app.requestSingleInstanceLock()
if (!acquiredLock) {
  app.quit()
} else if (modelSettingsSmokeOnly) {
  app.whenReady().then(async () => {
    const dataDir = process.env.HSC_DATA_DIR
    if (!dataDir) {
      console.error('[model-settings-smoke] HSC_DATA_DIR is required')
      app.exit(2)
      return
    }
    await mkdir(dataDir, { recursive: true })
    const result = await runModelSettingsSmoke(dataDir)
    await writeFile(join(dataDir, 'model-settings-smoke.json'), JSON.stringify(result), { encoding: 'utf8', mode: 0o600 })
    app.exit(Object.values(result).every(Boolean) ? 0 : 3)
  }).catch((error) => {
    console.error(`[model-settings-smoke] ${String(error)}`)
    app.exit(4)
  })
} else if (hardwareInventorySmokeOnly) {
  app.whenReady().then(async () => {
    const dataDir = process.env.HSC_DATA_DIR
    if (!dataDir) {
      console.error('[hardware-inventory-smoke] HSC_DATA_DIR is required')
      app.exit(2)
      return
    }
    const inventory = await collectVideoHardwareInventory()
    if (inventory.source === 'windows_inventory_failed') {
      console.error('[hardware-inventory-smoke] Windows inventory failed')
      app.exit(3)
      return
    }
    await mkdir(dataDir, { recursive: true })
    await writeFile(join(dataDir, 'hardware-inventory-smoke.json'), JSON.stringify(inventory), { encoding: 'utf8', mode: 0o600 })
    app.exit(inventory.source === 'windows_pnp_cim_v1' ? 0 : 4)
  }).catch((error) => {
    console.error(`[hardware-inventory-smoke] ${String(error)}`)
    app.exit(5)
  })
} else if (removeAutoLaunchOnly) {
  app.whenReady().then(async () => {
    app.setAppUserModelId(runtimeProfile.mode === 'trial' ? 'cn.family.homestudycompanion.trial' : 'cn.family.homestudycompanion')
    if (runtimeProfile.mode === 'formal') app.setLoginItemSettings({ openAtLogin: false, enabled: false })
    await unlink(autoLaunchMarker()).catch(() => undefined)
    app.quit()
  })
} else {
  app.whenReady().then(() => {
    app.setAppUserModelId(runtimeProfile.mode === 'trial' ? 'cn.family.homestudycompanion.trial' : 'cn.family.homestudycompanion')
    // Native ownership survives interrupted backend activities and is independent
    // of activeControlledGame GET. Unknown ownership blocks open and quit.
    void controlledGame.initialize()
    void Promise.resolve().then(() => ownedMedia().recover()).catch(() => { /* Unreadable ownership stays blocked at open/quit. */ })
    const menu = Menu.getApplicationMenu() ?? Menu.buildFromTemplate([
      { role: process.platform === 'darwin' ? 'appMenu' : 'fileMenu' },
      { role: 'editMenu' }, { role: 'viewMenu' }, { role: 'windowMenu' },
    ])
    menu.append(new MenuItem({ label: '家长', submenu: [parentEntryMenuItem(parentEntry.request)] }))
    Menu.setApplicationMenu(menu)
    ipcMain.on(PARENT_ENTRY_READY, (event) => parentEntry.ready(event.sender.id))
    registerParentDraftRecoveryIpc(ipcMain, draftRecoveryOwner, {
      prepare: (...args) => ownedDrafts().prepare(...args), read: (...args) => ownedDrafts().read(...args),
      checkpoint: (...args) => ownedDrafts().checkpoint(...args), discard: (...args) => ownedDrafts().discard(...args),
      revokeSender: senderId => draftRecovery?.revokeSender(senderId)
    })
    if (process.platform === 'win32' && app.isPackaged && runtimeProfile.mode === 'formal') {
      const initialized = autoLaunchMarker()
      if (!existsSync(initialized)) {
        app.setLoginItemSettings({ openAtLogin: true, enabled: true })
        if (app.getLoginItemSettings().openAtLogin) {
          void writeFile(initialized, new Date().toISOString(), { encoding: 'utf8', mode: 0o600 })
        }
      }
    }
    modelSettings = new ModelSettingsStore()
    asrSettings = new CloudAsrSettingsStore()
    voiceSettings = new VoiceSettingsStore()
    qwenRealtimeSettings = new QwenRealtimeSettingsStore()
    const voiceOnboarding = new VoiceOnboardingStore(app.getPath('userData'))
    voiceEvaluation = new VoiceEvaluationCatalog(process.env.HSC_VOICE_EVAL_ROOT)
    dataEncryptionKeyStore = new DataEncryptionKeyStore()
    reportDeliverySecrets = new ReportDeliverySecretStore()
    const householdSetupDefaultsPort = createHouseholdSetupDefaultsPort(app.getPath('userData'))
    ipcMain.handle('household:get-setup-defaults', async (event) => {
      if (!primaryWindow || primaryWindow.isDestroyed() || event.sender.id !== primaryWindow.webContents.id) return null
      return getHouseholdSetupDefaults(app.getPath('userData'), reportDeliverySecrets)
    })
    session.defaultSession.setPermissionCheckHandler((webContents, permission) =>
      permission === 'media' && Boolean(webContents && allowedMediaWindows.has(webContents.id))
    )
    session.defaultSession.setPermissionRequestHandler((webContents, permission, callback) => {
      callback(permission === 'media' && allowedMediaWindows.has(webContents.id))
    })

    ipcMain.handle('runtime:get-config', () => localService.getRuntime())
    ipcMain.handle('mistake-book:print', createMistakeBookPrintHandler(() => primaryWindow, (details, correlationId) => {
      void diagnosticRecorder.record({ source: 'main', event: 'mistake.print.stage', correlationId, details,
        level: ['failed', 'unconfirmed'].includes(details.print_outcome) ? 'warn' : 'info' }).catch(() => undefined)
    }))
    ipcMain.handle('diagnostics:get-summary', () => diagnosticRecorder.summary())
    ipcMain.handle('diagnostics:reveal', async () => {
      const error = await shell.openPath(diagnosticRecorder.rootPath)
      if (error) throw new Error(`诊断目录无法打开：${error}`)
      void diagnosticRecorder.record({ source: 'main', event: 'diagnostic_folder_revealed' })
      return { opened: true }
    })
    ipcMain.handle('diagnostics:renderer-event', (ipcEvent, input: DiagnosticEvent) => {
      if (!primaryWindow || primaryWindow.isDestroyed() || ipcEvent.sender.id !== primaryWindow.webContents.id) return { recorded: false }
      if (!input || typeof input.event !== 'string' || input.event.length > 120) return { recorded: false }
      void diagnosticRecorder.record({
        source: 'renderer',
        event: input.event,
        level: input.level,
        message: typeof input.message === 'string' ? input.message : undefined,
        correlationId: typeof input.correlationId === 'string' ? input.correlationId : undefined,
        details: input.details
      })
      return { recorded: true }
    })
    ipcMain.handle('runtime:restart', async (ipcEvent) => {
      const window = primaryWindow
      const external = process.env.HSC_LOCAL_API_URL
      const assertCurrent = (): void => {
        if (!window || window.isDestroyed() || primaryWindow !== window || ipcEvent.sender.id !== window.webContents.id
          || process.env.HSC_LOCAL_API_URL !== external) throw new Error('CLOUD_PROVIDER_REFRESH_SCOPE_CHANGED')
      }
      assertCurrent()
      if (external) {
        await localService.restart()
        assertCurrent()
        return localService.getRuntime()
      }
      return cloudProviders.reload({
        assertCurrent,
        fingerprint: () => cloudProviderSettingsFingerprint(app.getPath('userData')),
        loadSnapshot: async () => {
          const [modelConfig, asrConfig, realtimeConfig, tutorExplanationConfig] = await Promise.all([
            modelSettings.load(), asrSettings.load(), qwenRealtimeSettings.load(), loadTutorExplanationConfig(app.getPath('userData')),
          ])
          return { modelConfig, asrConfig, tutorExplanationConfig, voiceConfig: await voiceSettings.loadEffective(realtimeConfig) }
        },
        admit: () => localService.admitCloudProviderRefresh(),
        apply: (snapshot, admission) => localService.restartCloudProviderSnapshot(snapshot, admission),
        applied: (token) => localService.cloudProviderSnapshotApplied(token),
        result: () => localService.getRuntime(),
      })
    })
    ipcMain.handle('runtime:get-auto-launch', () => app.getLoginItemSettings())
    ipcMain.handle('runtime:set-auto-launch', (_, enabled: boolean) => {
      if (runtimeProfile.mode === 'trial') throw new Error('TRIAL_AUTO_LAUNCH_FORBIDDEN')
      app.setLoginItemSettings({ openAtLogin: enabled, enabled })
      return app.getLoginItemSettings()
    })
    ipcMain.on('runtime:set-awake-owner', (event, request: unknown) => runtimeAwake.handle(event, request))
    ipcMain.handle('report-delivery:save-secrets', async (_, values: ReportDeliverySecrets) => {
      const saved = await reportDeliverySecrets.save(validateReportDeliverySecrets(values))
      // Do not restart here: the renderer immediately supplies the same secret
      // to the current loopback agent for this run; the safe-store value is
      // injected by LocalServiceManager on every later service start.
      localService.setReportDeliverySecrets(saved)
      return { smtpPasswordConfigured: Boolean(saved.smtpPassword), wecomWebhookConfigured: Boolean(saved.wecomWebhook) }
    })
    ipcMain.handle('report-delivery:clear-secrets', async (_, names?: Array<keyof ReportDeliverySecrets>) => {
      if (names !== undefined && (!Array.isArray(names)
        || Array.from(names).some(name => name !== 'smtpPassword' && name !== 'wecomWebhook'))) {
        throw new Error('无效的送达通道凭据删除请求。')
      }
      const before = (await reportDeliverySecrets.inspect()).summary
      const suppressed: Array<keyof ReportDeliverySecrets> = names ?? ['smtpPassword', 'wecomWebhook']
      if (suppressed.length) await suppressHouseholdProvisioningSlots(app.getPath('userData'), suppressed)
      const saved = await reportDeliverySecrets.clear(names)
      localService.setReportDeliverySecrets(saved)
      const changed = (names ?? ['smtpPassword', 'wecomWebhook']).some((name) =>
        name === 'smtpPassword' ? before.smtpPasswordConfigured : before.wecomWebhookConfigured
      )
      // This is the one atomic desktop boundary: once safe storage is cleared,
      // restart the child exactly once so its inherited environment cannot keep
      // a prior credential alive. Repeated clear requests are harmless no-ops.
      if (changed) await localService.restart()
      return { smtpPasswordConfigured: Boolean(saved.smtpPassword), wecomWebhookConfigured: Boolean(saved.wecomWebhook) }
    })
    ipcMain.handle('report-delivery:clear-channel', async (_, channel: string, parentToken: string) => {
      if (!['email', 'wechat_work_bot'].includes(channel) || !parentToken) throw new Error('无效的外发通道删除请求。')
      const secretName: keyof ReportDeliverySecrets = channel === 'email' ? 'smtpPassword' : 'wecomWebhook'
      const before = (await reportDeliverySecrets.inspect()).summary
      await suppressHouseholdProvisioningSlots(app.getPath('userData'), [secretName])
      const saved = await reportDeliverySecrets.clear([secretName])
      localService.setReportDeliverySecrets(saved)
      const changed = secretName === 'smtpPassword' ? before.smtpPasswordConfigured : before.wecomWebhookConfigured
      const runtime = localService.getRuntime()
      let metadataCleared = false
      let runtimeRestarted = false
      let runtimeRestartFailed = false
      try {
        const response = await fetch(`${runtime.localApiUrl}/v1/parent/report-delivery/channels/${channel}`, {
          method: 'DELETE',
          headers: {
            ...(runtime.sessionToken ? { 'X-HSC-Session': runtime.sessionToken } : {}),
            'X-HSC-Parent': parentToken
          },
          signal: AbortSignal.timeout(5_000)
        })
        metadataCleared = response.ok
      } catch {
        metadataCleared = false
      } finally {
        if (changed) {
          try {
            await localService.restart()
            runtimeRestarted = true
          } catch {
            runtimeRestartFailed = true
          }
        }
      }
      return {
        smtpPasswordConfigured: Boolean(saved.smtpPassword),
        wecomWebhookConfigured: Boolean(saved.wecomWebhook),
        credentialsCleared: changed,
        metadataCleared,
        runtimeRestarted,
        runtimeRestartFailed
      }
    })
    ipcMain.handle('report-delivery:get-secret-status', async () => (await reportDeliverySecrets.inspect()).summary)
    ipcMain.handle('hardware:list-video-devices', () => collectVideoHardwareInventory())
    ipcMain.handle('hardware:get-camera-permission-status', () =>
      normalizeCameraPermissionStatus(systemPreferences.getMediaAccessStatus('camera'))
    )
    ipcMain.handle('knowledge:choose-seyk-root', async () => {
      const selected = await dialog.showOpenDialog({
        title: '选择苏e优课知识库文件夹',
        properties: ['openDirectory']
      })
      if (selected.canceled || !selected.filePaths[0]) return null
      const rootPath = selected.filePaths[0]
      const databasePath = join(rootPath, 'seyk_kb.db')
      const transcriptRoot = join(rootPath, 'transcripts')
      const videoRoot = join(rootPath, 'videos')
      const found = await Promise.all([databasePath, transcriptRoot, videoRoot].map(async (path) => {
        try { await access(path); return true } catch { return false }
      }))
      return { rootPath, databasePath, transcriptRoot, videoRoot, valid: found.every(Boolean) }
    })
    ipcMain.handle('knowledge:choose-standards-root', async () => {
      const selected = await dialog.showOpenDialog({
        title: '选择官方课程标准文件夹',
        properties: ['openDirectory']
      })
      if (selected.canceled || !selected.filePaths[0]) return null
      const rootPath = selected.filePaths[0]
      try {
        await access(join(rootPath, 'manifest.json'))
        return { rootPath, valid: true }
      } catch {
        return { rootPath, valid: false }
      }
    })
    ipcMain.handle('knowledge:open-local-path', async (_, path: string) => {
      if (typeof path !== 'string' || path.length < 1 || path.length > 4096) throw new Error('本地路径无效。')
      const error = await shell.openPath(path)
      if (error) throw new Error('无法打开本地课程视频。')
      return { opened: true }
    })
    ipcMain.handle('controlled-launch:prepare',(_,scope:ControlledLaunchScope)=>ownedLaunches().prepareForBackend(scope))
    ipcMain.handle('controlled-launch:list',(_,scope:ControlledLaunchScope)=>ownedLaunches().list(scope))
    ipcMain.handle('controlled-launch:cancel',(_,intentId:string)=>ownedLaunches().cancel(intentId))
    ipcMain.handle('controlled-launch:finish',(_,kind:'video'|'game',activityId:string)=>ownedLaunches().finishActivity(kind,activityId))
    ipcMain.handle('native-privacy:execute',(_,command:NativePrivacyCommand,token:string)=>ownedPrivacy().execute(command,token))
    ipcMain.handle('writing:capture-original',(event,input:import('../shared/nativeWriting').NativeWritingInput)=>{
      if(event.senderFrame!==event.sender.mainFrame)throw Error('原稿必须来自当前主窗口。')
      return captureNativeWriting(input,localService.getRuntime(),(value,time)=>localService.signNativeWriting(value,time))
    })
    ipcMain.handle('native-privacy:recover',(_,requestId:string,token:string,explicit=false)=>ownedPrivacy().recover(requestId,token,explicit===true))
    ipcMain.handle('controlled-media:open', (_, values: { path: string; title: string; maxSeconds: number; contentSessionId?: string; nativeIntentId?:string }) => {
      if(values.nativeIntentId)return ownedLaunches().dispatch(values.nativeIntentId,values.contentSessionId??'',()=>openControlledMedia(values))
      ownedLaunches().assertLegacyDispatch(values.contentSessionId??'')
      return openControlledMedia(values)
    })
    ipcMain.handle('controlled-media:close', (_, reason?: string, expectedSessionId?: string) =>
      typeof expectedSessionId === 'string' && expectedSessionId.trim() ? closeControlledTarget('video',reason,expectedSessionId) : { closed: false, proof: 'unknown' }
    )
    ipcMain.handle('controlled-game:choose-executable', async () => {
      const selected = await dialog.showOpenDialog({
        title: '选择允许孩子启动的游戏程序',
        properties: ['openFile'],
        filters: [{ name: 'Windows 应用程序', extensions: ['exe'] }],
      })
      if (selected.canceled || !selected.filePaths[0]) return null
      return { path: resolve(selected.filePaths[0]) }
    })
    ipcMain.handle('privacy:choose-portable-backup', async () => {
      const selected = await dialog.showOpenDialog({
        title: '选择家庭学习伙伴迁移备份',
        properties: ['openFile'],
        filters: [{ name: '家庭学习伙伴备份', extensions: ['hscbackup'] }],
      })
      if (selected.canceled || !selected.filePaths[0]) return null
      return { path: resolve(selected.filePaths[0]) }
    })
    ipcMain.handle('privacy:reveal-local-file', async (_, path: string) => {
      if (typeof path !== 'string' || path.length < 1 || path.length > 4096) throw new Error('本地路径无效。')
      shell.showItemInFolder(resolve(path))
      return { revealed: true }
    })
    ipcMain.handle('controlled-game:open', (_, values: ControlledGameLaunch&{nativeIntentId?:string}) => {
      if(values.nativeIntentId)return ownedLaunches().dispatch(values.nativeIntentId,values.gameSessionId,()=>controlledGame.open(values))
      ownedLaunches().assertLegacyDispatch(values.gameSessionId)
      return controlledGame.open(values)
    })
    ipcMain.handle('controlled-game:close', (_, reason?: string, expectedSessionId?: string) =>
      typeof expectedSessionId === 'string' && expectedSessionId.trim() ? closeControlledTarget('game',reason,expectedSessionId) : { closed: false, proof: 'unknown' })
    const petAudioSettings = new PetAudioSettingsStore()
    ipcMain.handle('pet-audio:settings', createPetAudioSettingsHandler(petAudioSettings, async (senderId, childId, parentToken) => {
      if (!primaryWindow || primaryWindow.isDestroyed() || primaryWindow.webContents.id !== senderId) throw Error('原音配置窗口无效。')
      const before = localService.getRuntime(), url = new URL(before.localApiUrl)
      if (before.status !== 'ready' || !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) throw Error('本地服务尚未就绪。')
      const response = await fetch(`${before.localApiUrl}/v1/parent/children/${encodeURIComponent(childId)}/pet-audio/authorization`, {
        headers: { 'X-HSC-Session': before.sessionToken, 'X-HSC-Parent': parentToken },
        redirect: 'error', signal: AbortSignal.timeout(5000)
      })
      if (!response.ok) throw Error('家长权限或孩子状态已变化，未保存配置。')
      const body = await response.json() as { child_id?: string }
      const after = localService.getRuntime()
      if (body.child_id !== childId || after.status !== 'ready' || after.sessionToken !== before.sessionToken
        || after.localApiUrl !== before.localApiUrl || !primaryWindow || primaryWindow.isDestroyed()
        || primaryWindow.webContents.id !== senderId) throw Error('本地服务或窗口已变化，未保存配置。')
    }, {
      apply: async config => { localService.setPetAudioConfig(config); await localService.restart() },
      revoke: () => localService.revokePetAudioConfig(),
      applied: actionId => localService.petAudioConfigApplied(actionId),
      canApply: () => !process.env.HSC_LOCAL_API_URL,
      importCatalog: async () => {
        if (!primaryWindow || primaryWindow.isDestroyed()) throw Error('家长窗口已关闭。')
        const chosen = await dialog.showOpenDialog(primaryWindow, { title: '导入维护者签名的已核型号与费率目录', properties: ['openFile'], filters: [{ name: '已核目录包', extensions: ['hscatalog'] }] })
        if (chosen.canceled || chosen.filePaths.length !== 1) return null
        return readPetCatalogImport(chosen.filePaths[0])
      }
    }))
    let rotationRecoveryHeld = false
    const requireNormalCloudRuntime = (): void => { if (rotationRecoveryHeld || localService.getRuntime().rotationAuthOnly) throw new Error('ROTATION_AUTH_ONLY') }
    ipcMain.handle('model:get-settings', async () => {
      const { config, diagnostic: loadDiagnostic } = await modelSettings.loadWithStatus()
      return config
        ? { loadDiagnostic, configured: true, provider: config.provider, baseUrl: config.baseUrl, model: config.model, voiceIntentModel: config.voiceIntentModel, fallbackConfigured: Boolean(config.fallback), fallbackProvider: config.fallback?.provider, fallbackBaseUrl: config.fallback?.baseUrl, fallbackModel: config.fallback?.model }
        : { loadDiagnostic, configured: false, provider: 'qwen', baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen3.7-plus', voiceIntentModel: 'qwen3.8-flash', fallbackConfigured: false }
    })
    ipcMain.handle('voice:get-settings', async () => summarizeVoiceConfig(await voiceSettings.loadEffective(await qwenRealtimeSettings.load())))
    ipcMain.handle('voice:realtime-get-settings', async () => summarizeQwenRealtime(await qwenRealtimeSettings.load()))
    const readVoiceRuntimePolicy = createVoiceRuntimePolicyHandler({
      allowedSender: event => !!primaryWindow && !primaryWindow.isDestroyed() && event.sender.id === primaryWindow.webContents.id,
      current: () => {
        const admission = localService.admitCloudProviderRefresh(), runtime = localService.getRuntime()
        if (admission.epoch !== runtime.runtimeEpoch) throw Error('VOICE_RUNTIME_POLICY_UNAVAILABLE')
        return {...runtime,child:admission.child,revision:admission.revision}
      },
    })
    ipcMain.handle('voice:get-runtime-policy', readVoiceRuntimePolicy)
    const nativeRealtimeCapabilities = new NativeRealtimeCapabilityGateway({
      allowed: event => !!primaryWindow && !primaryWindow.isDestroyed() && event.sender.id === primaryWindow.webContents.id,
      current: () => {
        const admission = localService.admitCloudProviderRefresh(), runtime = localService.getRuntime()
        if (admission.epoch !== runtime.runtimeEpoch) throw Error('NATIVE_REALTIME_REQUEST_UNCONFIRMED')
        return { ...runtime, child: admission.child, revision: admission.revision }
      },
      policy: event => readVoiceRuntimePolicy(event),
      sign: proof => localService.signNativeRealtimeCapability(proof),
    })
    const nativeSocket = new QwenNativeSocketRelay({
      allowed: event => !!primaryWindow && !primaryWindow.isDestroyed() && event.sender.id === primaryWindow.webContents.id,
      current: (event, id, sessionId) => nativeRealtimeCapabilities.isCurrent(event, id, sessionId),
      register: event => nativeRealtimeCapabilities.register(event),
      confirmModel: (event, id, sessionId, model) => nativeRealtimeCapabilities.confirmSocketModel(event, id, sessionId, model),
      onOpenFailure: (socket_open_stage, socket_open_failure, socket_open_retirement, socket_payload_failure, shape) => {
        void diagnosticRecorder.record({ source: 'main', event: 'voice.realtime.socket_open_failed', level: 'warn',
          details: { socket_open_stage, socket_open_failure, socket_open_retirement, socket_payload_failure, ...(shape ?? {}) } })
      },
      onMessageShapeFailure: shape => {
        void diagnosticRecorder.record({ source: 'main', event: 'voice.realtime.socket_message_rejected', level: 'warn', details: shape })
      },
      onEstablishedEnd: details => {
        void diagnosticRecorder.record({ source: 'main', event: 'voice.realtime.socket_ended',
          level: details.socket_end_kind === 'local_close' ? 'info' : 'warn', details })
      },
      retire: (event, id) => nativeRealtimeCapabilities.close(event, id),
      policy: event => readVoiceRuntimePolicy(event),
      config: () => qwenRealtimeSettings.load(),
      emit: (event, packet) => {
        const win = primaryWindow
        if (win && !win.isDestroyed() && event.sender.id === win.webContents.id) {
          win.webContents.send('voice:native-socket-event', packet)
        }
      },
    })
    const nativeRealtimeOwners = new Set<number>()
    const trackNativeRealtimeOwner = (sender: Electron.WebContents): void => {
      if (nativeRealtimeOwners.has(sender.id)) return
      nativeRealtimeOwners.add(sender.id)
      const retire = (): void => { nativeSocket.retireOwner(sender.id); nativeRealtimeCapabilities.retireOwner(sender.id) }
      sender.on('did-start-navigation', (_event, _url, inPlace, mainFrame) => { if (mainFrame && !inPlace) retire() })
      sender.on('render-process-gone', retire)
      sender.once('destroyed', () => { retire(); nativeRealtimeOwners.delete(sender.id) })
    }
    ipcMain.handle('voice:realtime-native-runtime', (event, request: import('../shared/nativeRealtimeCapability').NativeRealtimeForward) =>
      nativeRealtimeCapabilities.forward(event, request))
    ipcMain.handle('voice:realtime-native-close', (event, id: string) => nativeRealtimeCapabilities.close(event, id))
    ipcMain.handle('voice:native-socket-open', (event, sessionId: string) => {
      trackNativeRealtimeOwner(event.sender)
      return nativeSocket.open(event, sessionId)
    })
    ipcMain.handle('voice:native-socket-send', (event, id: string, raw: string) => nativeSocket.send(event, id, raw))
    ipcMain.handle('voice:native-socket-close', (event, id: string) => nativeSocket.close(event, id))
    ipcMain.handle('voice:realtime-save-settings', async (ipcEvent, config: QwenRealtimeConfig) => cloudProviders.mutate(async () => {
      requireNormalCloudRuntime()
      if (!primaryWindow || primaryWindow.isDestroyed() || ipcEvent.sender.id !== primaryWindow.webContents.id) throw new Error('实时语音配置窗口无效。')
      const apiKey = await qwenRealtimeSettings.resolveApiKey(config.apiKey, {
        keychainService: process.env.HSC_QWEN_REALTIME_KEYCHAIN_SERVICE?.trim() || (process.platform === 'darwin' ? 'hsc-qwen-realtime-api-key' : undefined),
        environmentKey: process.env.HSC_QWEN_REALTIME_API_KEY,
      })
      const resolved = validateQwenRealtimeConfig({ ...config, apiKey })
      await testQwenRealtimeConfig(resolved)
      const saved = await qwenRealtimeSettings.save(resolved)
      localService.setVoiceConfig(await voiceSettings.loadEffective(saved))
      await restartChangedCloudProviders()
      return summarizeQwenRealtime(saved)
    }))
    ipcMain.handle('voice:realtime-test-settings', async (ipcEvent, config: QwenRealtimeConfig) => {
      requireNormalCloudRuntime()
      if (!primaryWindow || primaryWindow.isDestroyed() || ipcEvent.sender.id !== primaryWindow.webContents.id) throw new Error('实时语音配置窗口无效。')
      const apiKey = await qwenRealtimeSettings.resolveApiKey(config.apiKey, {
        keychainService: process.env.HSC_QWEN_REALTIME_KEYCHAIN_SERVICE?.trim() || (process.platform === 'darwin' ? 'hsc-qwen-realtime-api-key' : undefined),
        environmentKey: process.env.HSC_QWEN_REALTIME_API_KEY,
      })
      return testQwenRealtimeConfig(validateQwenRealtimeConfig({ ...config, apiKey }))
    })
    ipcMain.handle('voice:realtime-clear-settings', async (ipcEvent) => cloudProviders.mutate(async () => {
      requireNormalCloudRuntime()
      if (!primaryWindow || primaryWindow.isDestroyed() || ipcEvent.sender.id !== primaryWindow.webContents.id) throw new Error('实时语音配置窗口无效。')
      await suppressHouseholdProvisioningSlots(app.getPath('userData'), ['qwenRealtime'])
      await qwenRealtimeSettings.clear()
      localService.setVoiceConfig(await voiceSettings.loadEffective(null))
      await restartChangedCloudProviders()
      return summarizeQwenRealtime(null)
    }))
    ipcMain.handle('voice:realtime-negotiate', async (ipcEvent, offerSdp: string) => {
      const window = primaryWindow
      const sender = ipcEvent.sender
      const samePrimary = (): boolean => !!window && !window.isDestroyed() && primaryWindow === window
        && sender === window.webContents && !sender.isDestroyed()
      if (!samePrimary()) throw new Error('实时语音会话窗口无效。')
      return cloudProviders.withCurrentProviderRequest(async providerSignal => {
        const ownerAbort = new AbortController()
        const signal = AbortSignal.any([providerSignal, ownerAbort.signal])
        const abortOwner = (): void => { ownerAbort.abort(new DOMException('语音连接已取消，请重新连接。', 'AbortError')) }
        const navigation = (_event: unknown, _url: string, isInPlace: boolean, isMainFrame: boolean): void => {
          if (isMainFrame && !isInPlace) abortOwner()
        }
        sender.on('did-start-navigation', navigation)
        sender.on('destroyed', abortOwner)
        sender.on('render-process-gone', abortOwner)
        const current = (): void => {
          if (!samePrimary()) abortOwner()
          signal.throwIfAborted()
        }
        try {
          current()
          const config = await qwenRealtimeSettings.load()
          current()
          if (!config?.enabled) throw new Error('实时语音尚未配置。')
          const started = Date.now()
          try {
            current()
            const policy = await readVoiceRuntimePolicy(ipcEvent)
            current()
            if (policy.capture !== 'configured') throw new Error('实时语音当前未获音频云端许可。')
            const result = await exchangeQwenRealtimeSdp(config, offerSdp, undefined, signal)
            current()
            const latestPolicy = await readVoiceRuntimePolicy(ipcEvent)
            current()
            if (latestPolicy.capture !== 'configured' || latestPolicy.runtimeEpoch !== policy.runtimeEpoch) {
              throw new Error('实时语音音频许可或本机会话已变化，请重新连接。')
            }
            void diagnosticRecorder.record({ source: 'main', event: 'voice.realtime.signaling_succeeded', details: { model: result.model, latency_ms: Date.now() - started } })
            const nativeHostConnectionId = nativeRealtimeCapabilities.register(ipcEvent)
            trackNativeRealtimeOwner(sender)
            return { ...result, nativeHostConnectionId }
          } catch (error) {
            void diagnosticRecorder.record({ source: 'main', event: 'voice.realtime.signaling_failed', level: 'error', details: { model: config.model, latency_ms: Date.now() - started, error } })
            throw error
          }
        } catch (error) {
          current()
          throw error
        } finally {
          sender.removeListener('did-start-navigation', navigation)
          sender.removeListener('destroyed', abortOwner)
          sender.removeListener('render-process-gone', abortOwner)
        }
      })
    })
    let publicSpeech: { ownerId: number; token: string; origin: string; service: PublicReplySpeechService } | null = null
    const publicSpeechOwners = new Set<number>()
    const publicSpeechFor = (senderId: number): PublicReplySpeechService => {
      if (!primaryWindow || primaryWindow.isDestroyed() || primaryWindow.webContents.id !== senderId) throw Error('PUBLIC_REPLY_WINDOW_INVALID')
      if (!publicSpeechOwners.has(senderId)) {
        publicSpeechOwners.add(senderId)
        const contents = primaryWindow.webContents
        const retireOwner = () => {
          if (publicSpeech?.ownerId !== senderId) return
          const old = publicSpeech; publicSpeech = null
          void old.service.close()
        }
        contents.on('did-start-navigation', (_event, _url, isInPlace, isMainFrame) => {
          if (isMainFrame && !isInPlace) retireOwner()
        })
        contents.on('render-process-gone', retireOwner)
        contents.once('destroyed', () => { retireOwner(); publicSpeechOwners.delete(senderId) })
      }
      const runtime = localService.getRuntime(), url = new URL(runtime.localApiUrl)
      if (runtime.status !== 'ready' || !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) throw Error('PUBLIC_REPLY_RUNTIME_UNAVAILABLE')
      if (publicSpeech?.ownerId === senderId && publicSpeech.token === runtime.sessionToken && publicSpeech.origin === runtime.localApiUrl) return publicSpeech.service
      if (publicSpeech) void publicSpeech.service.close()
      const isCurrent = () => {
        const now = localService.getRuntime()
        return !!primaryWindow && !primaryWindow.isDestroyed() && primaryWindow.webContents.id === senderId
          && publicSpeech?.service === service
          && now.status === 'ready' && now.sessionToken === runtime.sessionToken && now.localApiUrl === runtime.localApiUrl
      }
      const service = new PublicReplySpeechService({ isCurrent,
        config: () => qwenRealtimeSettings.load(),
        authority: command => executeNativeRealtimeOutput(command, runtime, value => localService.signNativeRealtimeOutput(value)),
        context: async source => {
          const response = await fetch(`${runtime.localApiUrl}/v1/sessions/${encodeURIComponent(source.sessionId)}/voice-output-context/${encodeURIComponent(source.agentTurnId)}`,
            { headers: { 'X-HSC-Session': runtime.sessionToken }, redirect: 'error', signal: AbortSignal.timeout(5000) })
          if (response.status === 409) return { status: 'unavailable' }
          if (!response.ok || Number(response.headers.get('Content-Length')) > 4096) throw Error('PUBLIC_REPLY_CONTEXT_UNCONFIRMED')
          return await response.json() as Record<string, unknown>
        },
        onRetired: leaseId => { if (isCurrent()) primaryWindow!.webContents.send('voice:public-reply-retired', leaseId) },
        onFailure: code => { void diagnosticRecorder.record({ source: 'main', event: 'voice.public_reply.failed', level: 'warn', details: { code } }) },
        onUsage: usage => { void diagnosticRecorder.record({ source: 'main', event: 'voice.public_reply.usage', details: {
          input_tokens: usage.inputTokens, output_tokens: usage.outputTokens, total_tokens: usage.totalTokens,
        } }) },
      })
      publicSpeech = { ownerId: senderId, token: runtime.sessionToken, origin: runtime.localApiUrl, service }
      return service
    }
    ipcMain.handle('voice:public-reply-open', (event, source: PublicReplySpeechSource) => publicSpeechFor(event.sender.id).open(source))
    ipcMain.handle('voice:public-reply-generate', (event, leaseId: string) => publicSpeechFor(event.sender.id).generate(leaseId))
    ipcMain.handle('voice:public-reply-settle', (event, leaseId: string, disposition: PublicReplySettlement) => publicSpeechFor(event.sender.id).settle(leaseId, disposition))
    ipcMain.handle('voice:public-reply-cancel', (event, leaseId: string) => publicSpeechFor(event.sender.id).cancel(leaseId))
    app.on('before-quit', () => { if (publicSpeech) void publicSpeech.service.close() })
    ipcMain.handle('voice:save-settings', async (_, config: VoiceConfig) => cloudProviders.mutate(async () => {
      requireNormalCloudRuntime()
      const normalized = await voiceSettings.save(validateVoiceConfig(config))
      localService.setVoiceConfig(normalized)
      await restartChangedCloudProviders()
      return summarizeVoiceConfig(normalized)
    }))
    ipcMain.handle('voice:clear-settings', async () => cloudProviders.mutate(async () => {
      requireNormalCloudRuntime()
      await voiceSettings.clear(); localService.setVoiceConfig(null); await restartChangedCloudProviders()
      return summarizeVoiceConfig(null)
    }))
    ipcMain.handle('voice:get-trials', () => voiceSettings.loadTrials())
    ipcMain.handle('voice:record-trial', (_, trial: VoiceTrial) => voiceSettings.addTrial(trial))
    ipcMain.handle('voice:get-listening-ratings', () => voiceSettings.loadListeningRatings())
    ipcMain.handle('voice:record-listening-rating', (_, rating: VoiceListeningRating) => voiceSettings.addListeningRating(rating))
    ipcMain.handle('voice:get-wake-word', (_, childId: string) => voiceSettings.loadWakeWord(childId))
    ipcMain.handle('voice:get-onboarding', (_, childId: string) => voiceOnboarding.load(childId))
    ipcMain.handle('voice:save-onboarding', async (_, progress: VoiceOnboarding) => {
      const saved = await voiceOnboarding.save(progress)
      void diagnosticRecorder.record({ source: 'main', event: 'voice.onboarding.progress_saved', details: { step: saved.step } })
      return saved
    })
    ipcMain.handle('voice:save-wake-word', async (_, settings: WakeWordSettings) => {
      const saved = await voiceSettings.saveWakeWord(validateWakeWordSettings(settings))
      void diagnosticRecorder.record({ source: 'main', event: 'voice.wake_word.saved' })
      return saved
    })
    ipcMain.handle('voice:list-evaluation-candidates', () => voiceEvaluation.list())
    ipcMain.handle('voice:read-evaluation-audio', (_, candidateId: string) => {
      if (typeof candidateId !== 'string' || candidateId.length > 120) throw new Error('评测声音候选无效。')
      return voiceEvaluation.audio(candidateId)
    })
    ipcMain.handle('voice:list-reference-samples', () => listReferenceVoiceSamples())
    ipcMain.handle('voice:enroll-reference', async (_, values: { sampleId?: string; apiKey?: string; rightsConfirmed?: boolean }) => cloudProviders.mutate(async () => {
      requireNormalCloudRuntime()
      if (!values || typeof values !== 'object' || typeof values.sampleId !== 'string' || !/^[a-f0-9]{24}$/.test(values.sampleId)) throw new Error('参考声音样本标识无效。')
      const previous = await voiceSettings.load()
      const existingAsr = await asrSettings.load()
      const reusableAsrKey = existingAsr?.provider === 'dashscope' && existingAsr.baseUrl.includes('dashscope.aliyuncs.com') ? existingAsr.apiKey : ''
      const apiKey = values.apiKey?.trim() || previous?.apiKey || reusableAsrKey || ''
      const enrolled = await enrollQwenReferenceVoice(values.sampleId, apiKey, values.rightsConfirmed === true)
      const normalized = await voiceSettings.save(validateVoiceConfig({ provider: 'qwen_voice_clone', relayUrl: '', apiKey, voiceId: enrolled.voiceId, model: QWEN_VOICE_CLONE_MODEL, region: 'cn-beijing' }))
      localService.setVoiceConfig(normalized)
      await restartChangedCloudProviders()
      return { ...summarizeVoiceConfig(normalized), ...enrolled }
    }))
    ipcMain.handle('voice:preview-settings', async (_, config: VoiceConfig) => {
      requireNormalCloudRuntime()
      denyUnpricedMoneyEgress('voice_config_preview')
      const normalized = validateVoiceConfig(config)
      const started = Date.now()
      const response = await fetch(normalized.relayUrl, {
        method: 'POST',
        headers: { Authorization: `Bearer ${normalized.apiKey}`, Accept: 'application/octet-stream', 'Content-Type': 'application/json', 'X-HSC-TTS-Protocol': 'pcm16-frame-v1' },
        body: JSON.stringify({ text: '你好，我们一起把今天的学习安排清楚。', voice_id: normalized.voiceId, format: 'pcm_s16le', sample_rate: 24000 }),
        signal: AbortSignal.timeout(10_000)
      })
      if (!response.ok || !response.body) throw new Error(`试听连接失败（HTTP ${response.status}）。请检查 relay、音色授权和密钥。`)
      const reader = response.body.getReader(); let pending = new Uint8Array(); const pcmParts: Uint8Array[] = []; let sampleRate: number | null = null; let firstFrameMs: number | null = null; let pcmBytes = 0
      try {
        while (true) {
          const next = await reader.read()
          if (next.done) break
          const joined = new Uint8Array(pending.length + next.value.length); joined.set(pending); joined.set(next.value, pending.length); pending = joined
          while (pending.length >= 8) {
            const view = new DataView(pending.buffer, pending.byteOffset, pending.byteLength); const frameRate = view.getUint32(0, true); const size = view.getUint32(4, true)
            if (![8000, 16000, 22050, 24000, 32000, 44100, 48000].includes(frameRate) || size <= 0 || size > 512000 || size % 2) throw new Error('relay 没有返回约定的 PCM 音频帧。')
            if (pending.length < 8 + size) break
            if (sampleRate === null) { sampleRate = frameRate; firstFrameMs = Date.now() - started } else if (sampleRate !== frameRate) throw new Error('relay 返回了混合采样率的试听音频。')
            const pcm = pending.slice(8, 8 + size); pcmBytes += pcm.length
            if (pcmBytes > 3_000_000) throw new Error('固定试听文案的音频异常过大。')
            pcmParts.push(pcm); pending = pending.slice(8 + size)
          }
        }
      } finally { reader.releaseLock() }
      if (sampleRate === null || firstFrameMs === null || !pcmParts.length || pending.length) throw new Error('relay 没有返回完整的可试听音频。')
      const pcm = Buffer.concat(pcmParts.map((part) => Buffer.from(part)))
      return { ok: true, firstFrameMs, totalMs: Date.now() - started, sampleRate, pcmBase64: pcm.toString('base64') }
    })
    ipcMain.handle('model:save-settings', async (_, config: ModelConfig) => cloudProviders.mutate(async () => {
      requireNormalCloudRuntime()
      const normalized = resolveModelConfigCredentials(config, await modelSettings.load())
      await modelSettings.save(normalized)
      localService.setModelConfig(normalized)
      await restartChangedCloudProviders()
      return { configured: true, provider: normalized.provider, baseUrl: normalized.baseUrl, model: normalized.model, voiceIntentModel: normalized.voiceIntentModel, fallbackConfigured: Boolean(normalized.fallback), fallbackProvider: normalized.fallback?.provider, fallbackBaseUrl: normalized.fallback?.baseUrl, fallbackModel: normalized.fallback?.model }
    }))
    ipcMain.handle('model:clear-settings', async () => cloudProviders.mutate(async () => {
      requireNormalCloudRuntime()
      await suppressHouseholdProvisioningSlots(app.getPath('userData'), ['model'])
      await modelSettings.clear()
      localService.setModelConfig(null)
      await restartChangedCloudProviders()
      return { configured: false, provider: 'qwen', baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen3.7-plus', voiceIntentModel: 'qwen3.8-flash', fallbackConfigured: false }
    }))
    ipcMain.handle('model:test-settings', async (_, config: ModelConfig) => {
      requireNormalCloudRuntime()
      denyUnpricedMoneyEgress('model_config_test')
      const normalized = validateModelConfig(config)
      const started = Date.now()
      const response = await fetch(`${normalized.baseUrl}/chat/completions`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${normalized.apiKey}`, 'Content-Type': 'application/json' },
        body: JSON.stringify(modelConnectionPayload(normalized)),
        signal: AbortSignal.timeout(15_000)
      })
      if (!response.ok) throw new Error(`连接测试失败（HTTP ${response.status}），请检查密钥、模型名和API地址。`)
      const body = await response.json() as { choices?: Array<{ message?: { content?: string } }> }
      if (!body.choices?.[0]?.message?.content) throw new Error('服务商返回了无法识别的结果。')
      return { ok: true, latencyMs: Date.now() - started, model: normalized.model }
    })

    const configureAndStart = (skipRotation: boolean, current: () => boolean = () => true) => dataEncryptionKeyStore.loadOrCreate().then(async (encodedDataKey) => {
      if (!current()) throw new Error('ROTATION_RECOVERY_CHANGED')
      if (!skipRotation && app.isPackaged && runtimeProfile.mode === 'formal') {
        const rotationScope = { resourcesPath: process.resourcesPath, userDataPath: app.getPath('userData') }
        // Resolve any interrupted generation before bootstrap may inspect slots.
        await recoverQwenKeyRotation(rotationScope)
        const imported = await importInstalledHouseholdSetup({ resourcesPath: process.resourcesPath,
          userDataPath: app.getPath('userData'), smtpPort: householdSetupDefaultsPort })
        if (imported.status === 'partial') throw new HouseholdProvisioningError('HOUSEHOLD_BOOTSTRAP_IO_UNAVAILABLE')
        await rotateInstalledQwenKey(rotationScope)
      }
      const keychainService = process.env.HSC_MODEL_KEYCHAIN_SERVICE?.trim()
      const loadModelConfig = keychainService
        ? modelSettings.loadOrImportDeepSeekFromMacOSKeychain(keychainService)
        : modelSettings.load()
      const asrKeychainService = process.env.HSC_ASR_KEYCHAIN_SERVICE?.trim()
      const loadAsrConfig = asrKeychainService
        ? asrSettings.loadOrImportDashScopeFromMacOSKeychain(asrKeychainService)
        : asrSettings.load()
      const realtimeKeychainService = process.env.HSC_QWEN_REALTIME_KEYCHAIN_SERVICE?.trim()
        || (process.platform === 'darwin' ? 'hsc-qwen-realtime-api-key' : undefined)
      const loadRealtimeConfig = qwenRealtimeSettings.loadOrImport({
        keychainService: realtimeKeychainService,
        environmentKey: process.env.HSC_QWEN_REALTIME_API_KEY,
        endpoint: process.env.HSC_QWEN_REALTIME_ENDPOINT,
      })
      const [config, asrConfig, realtimeConfig, deliverySecrets, tutorExplanationConfig] = await Promise.all([
        loadModelConfig, loadAsrConfig, loadRealtimeConfig, reportDeliverySecrets.load(),
        loadTutorExplanationConfig(app.getPath('userData'), app.isPackaged && runtimeProfile.mode === 'formal' ? process.resourcesPath : undefined),
      ])
      if (!current()) throw new Error('ROTATION_RECOVERY_CHANGED')
      const resolvedModelConfig = ensureDashScopeTextFallback(config, asrConfig)
        if (resolvedModelConfig && resolvedModelConfig !== config) await modelSettings.save(resolvedModelConfig)
        localService.setModelConfig(resolvedModelConfig)
      localService.setTutorExplanationConfig(tutorExplanationConfig)
        try { localService.setPetAudioConfig(petAudioSettings.runtimeConfig()) }
        catch { localService.setPetAudioConfig(null); console.warn('PET_AUDIO_PROTECTED_CONFIG_UNAVAILABLE') }
        localService.setAsrConfig(asrConfig)
        localService.setVoiceConfig(await voiceSettings.loadEffective(realtimeConfig))
        if (!current()) throw new Error('ROTATION_RECOVERY_CHANGED')
        if (realtimeConfig) void diagnosticRecorder.record({ source: 'main', event: 'voice.realtime.configured', details: { model: realtimeConfig.model, voice: realtimeConfig.voice, enabled: realtimeConfig.enabled } })
        localService.setDataEncryptionKey(encodedDataKey)
        localService.setReportDeliverySecrets(deliverySecrets)
      })
      .then(async () => { if (!current()) throw new Error('ROTATION_RECOVERY_CHANGED'); await localService.start(); if (!current()) { await localService.stop(); throw new Error('ROTATION_RECOVERY_CHANGED') }; if (app.isPackaged) {
        try { await importPilotConsent({ resourcesPath: process.resourcesPath, recipient: process.env.HSC_PILOT_TEST_RECIPIENT?.trim() || '', runtime: localService.getRuntime(), sign: (id, recipient) => localService.signPilotProvision(id, recipient) }) }
        catch { console.warn('PILOT_PROVISION_FAILED') }
      } if (!current()) { await localService.stop(); throw new Error('ROTATION_RECOVERY_CHANGED') }; pilotTestEmail.start() })
    const rotationScope = { resourcesPath: process.resourcesPath, userDataPath: app.getPath('userData') }
    const rotationRecovery = new QwenRotationRecovery({
      runtime: () => localService.getRuntime(),
      binding: () => qwenRotationRecoveryBinding(rotationScope),
      authorize: async (token, runtime) => {
        const url = new URL(runtime.localApiUrl)
        if (url.hostname !== '127.0.0.1') return false
        const response = await fetch(`${url.origin}/v1/parent/rotation-authority`, { method:'POST', cache:'no-store',
          headers:{'X-HSC-Session':runtime.sessionToken,'X-HSC-Parent':token}, signal:AbortSignal.timeout(10_000) })
        if (!response.ok) return false
        const value = await response.json() as unknown
        return !!value && typeof value === 'object' && !Array.isArray(value)
          && Object.keys(value).length === 1 && (value as {authorized?:unknown}).authorized === true
      },
      stopAuth: () => localService.stopRotationAuthOnly(),
      startOld: async current => {
        if (!current()) throw new Error('ROTATION_RECOVERY_CHANGED')
        localService.markRotationKeyUpdatePending(); await configureAndStart(true, current)
        if (!current()) throw new Error('ROTATION_RECOVERY_CHANGED')
        rotationRecoveryHeld = false; localService.setRotationRecoveryGuard(false)
      },
      cancelStarting: () => localService.stop(),
      restoreAuth: async () => {
        await localService.stop()
        if (localService.getRuntime().rotationAuthOnly) await localService.stopRotationAuthOnly()
        localService.markRotationKeyUpdatePending(false)
        await localService.startRotationAuthOnly()
      },
    })
    const rotationOwnerCurrent = (event: Electron.IpcMainInvokeEvent, window: BrowserWindow | null, frame: Electron.WebFrameMain | null): boolean => {
      if (!window || window.isDestroyed() || primaryWindow !== window || event.sender !== window.webContents
        || event.sender.isDestroyed() || frame !== window.webContents.mainFrame) return false
      try {
        const expected = new URL(app.isPackaged ? pathToFileURL(join(__dirname, '../renderer/index.html')).href : (process.env.ELECTRON_RENDERER_URL ?? ''))
        const actual = new URL(event.sender.getURL())
        return actual.protocol === expected.protocol && actual.host === expected.host && actual.pathname === expected.pathname && actual.search === expected.search
      } catch { return false }
    }
    ipcMain.handle('tutor-explanation:settings', (event, parentToken: string, command: TutorExplanationCommand) => {
      const window = primaryWindow, frame = event.senderFrame
      const current = (): boolean => rotationOwnerCurrent(event, window, frame)
      if (!current()) throw Error('TUTOR_EXPLANATION_PARENT_REQUIRED')
      requireNormalCloudRuntime()
      const authorize = createTutorExplanationAuthorizer({ senderId: event.sender.id, current,
        runtime: () => localService.getRuntime(), externalBackend: Boolean(process.env.HSC_LOCAL_API_URL) })
      const handler = createTutorExplanationHandler({ userData: app.getPath('userData'), authorize,
        apply: async config => { await localService.applyTutorExplanationConfig(config); return localService.getRuntime().runtimeEpoch } })
      return command?.action === 'save' ? cloudProviders.mutate(() => handler(event.sender.id, parentToken, command))
        : handler(event.sender.id, parentToken, command)
    })
    ipcMain.handle('rotation-recovery:continue', (event, token: string) => {
      const window = primaryWindow, frame = event.senderFrame
      const ownerCurrent = (): boolean => rotationOwnerCurrent(event, window, frame)
      if (!ownerCurrent()) throw new Error('ROTATION_RECOVERY_UNAVAILABLE')
      return cloudProviders.mutate(() => rotationRecovery.continueWithOldKey(token, ownerCurrent)).then(
        () => ({status:'completed' as const}),
        error => {
          if (error instanceof Error && error.message === 'ROTATION_RECOVERY_RESTART_REQUIRED') return {status:'restart_required' as const}
          throw new Error('ROTATION_RECOVERY_FAILED')
        },
      )
    })
    ipcMain.handle('rotation-recovery:cancel', event => {
      if (!rotationOwnerCurrent(event, primaryWindow, event.senderFrame)) throw new Error('ROTATION_RECOVERY_UNAVAILABLE')
      return rotationRecovery.cancel()
    })
    void cloudProviders.mutate(() => configureAndStart(false))
      .catch(async (error) => {
        if (error instanceof QwenRotationError && error.code === 'ROTATION_MODEL_UNCLASSIFIED') {
          try {
            rotationRecoveryHeld = true; localService.setRotationRecoveryGuard(true)
            await rotationRecovery.arm()
            localService.setDataEncryptionKey(await dataEncryptionKeyStore.loadOrCreate())
            await localService.startRotationAuthOnly()
            return
          } catch { localService.reportProtectedStartupFailure('rotation_model_unclassified'); return }
        }
        localService.reportProtectedStartupFailure(error instanceof MediaKeyRecoveryRequiredError ? 'media_key_recovery'
          : error instanceof Error && error.message === 'TUTOR_EXPLANATION_RECOVERY_MARKER_BLOCKED' ? 'tutor_recovery_marker' : 'storage'); console.error(error)
      })
      .finally(createWindow)
    app.on('activate', () => {
      if(quitFailureRecovery.focusIfActive())return
      if(draftShutdownFailed){showDraftShutdownFailure();return}
      if (!draftQuitDraining && BrowserWindow.getAllWindows().length === 0) createWindow()
    })
  })

  app.on('second-instance', () => {
    if(quitFailureRecovery.focusIfActive())return
    if(draftShutdownFailed){showDraftShutdownFailure();return}
    const window = BrowserWindow.getAllWindows()[0]
    if (!window) return
    if (window.isMinimized()) window.restore()
    window.focus()
  })

  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit()
  })

  const verifiedQuit = createVerifiedQuit([
    () => controlledGame.close('app_quit'),
    async () => {const closed=await closeControlledMedia('app_quit');return {...closed,closed:closed.closed&&!ownedLaunches().unresolved()}},
  ], async () => {
    quitFailureWatch?.(); quitFailureWatch = null
    runtimeAwake.clear()
    void diagnosticRecorder.record({ source: 'main', event: 'app_quit_requested' })
    pilotTestEmail.stop()
    try {
      await draftPreparation?.quiesce()
      if (acknowledgedAbnormalBackend?.()) {
        // Explicit acknowledgement of an already closed abnormal original is
        // not a successful save. Existing unclean-runtime recovery is retained.
        void diagnosticRecorder.record({ source: 'main', event: 'app_quit_backend_abnormal_acknowledged', level: 'warn', details: { error_code: 'LOCAL_SERVICE_SHUTDOWN_ABNORMAL' } })
      } else {
        acknowledgedAbnormalBackend = null
        await localService.stop()
        void diagnosticRecorder.record({ source: 'main', event: 'app_quit_service_closed' })
      }
    } catch (error) {
      const code = (error as { code?: unknown })?.code === 'LOCAL_SERVICE_SHUTDOWN_ABNORMAL'
        ? 'LOCAL_SERVICE_SHUTDOWN_ABNORMAL' : 'LOCAL_SERVICE_SHUTDOWN_PENDING'
      await diagnosticRecorder.record({ source: 'main', event: 'graceful_shutdown_failed', level: 'error', details: { error_code: code } }).catch(() => {})
      showServiceQuitFailure((error as { code?: unknown })?.code === 'LOCAL_SERVICE_SHUTDOWN_PENDING')
      return
    }
    quitFailureRecovery.clear()
    await diagnosticRecorder.flush()
    void diagnosticRecorder.record({ source: 'main', event: 'app_quit_diagnostics_flushed' })
    quitAfterServiceStop = true
    void diagnosticRecorder.record({ source: 'main', event: 'app_quit_final_requested' })
    app.quit()
  }, async () => {
    await diagnosticRecorder.record({ source: 'main', event: 'graceful_shutdown_failed', level: 'warn', details: { error_code: 'DEVICE_COMMAND_FAILED' } }).catch(() => {})
    quitFailureRecovery.show('external')
  })
  let quitDiagnosticsFinalized = false
  // will-quit runs after renderer windows/unload: their final sync checkpoint still owns this codec.
  app.on('will-quit', (event) => {
    if (quitDiagnosticsFinalized) return
    event.preventDefault()
    if (draftCodecClosing) return
    const dispose = draftCodecDispose
    draftCodecClosing = Promise.resolve().then(async () => {
      await diagnosticRecorder.record({ source: 'main', event: 'app_quit_will_quit' })
      if (dispose) {
        try { await dispose() }
        catch {
          quitAfterServiceStop=false;draftQuitDraining=true;draftShutdownFailed=true
          quitDiagnosticsFailed=false
          void diagnosticRecorder.record({source:'main',event:'draft_codec_dispose_failed',level:'error',details:{error_code:'DRAFT_RECOVERY_IO'}}).catch(()=>{})
          showDraftShutdownFailure()
          return
        }
        if (draftCodecDispose === dispose) draftCodecDispose = undefined
        draftShutdownFailed = false
        await diagnosticRecorder.record({ source: 'main', event: 'app_quit_codec_disposed' })
      }
      await diagnosticRecorder.flush()
      quitDiagnosticsFailed = false
      quitFailureRecovery.clear()
      quitDiagnosticsFinalized = true
      app.quit()
    }).catch(() => {
      // Diagnostic failure is not a codec-disposal failure or a saved receipt.
      // Only a manual retry may re-enter this final boundary, without stopping
      // a replacement backend or disposing an already confirmed codec again.
      quitAfterServiceStop=false;draftQuitDraining=true;quitDiagnosticsFailed=true
      void diagnosticRecorder.record({ source: 'main', event: 'graceful_shutdown_failed', level: 'error' }).catch(() => {})
      quitFailureRecovery.show('diagnostics')
    }).finally(() => { draftCodecClosing = null })
  })
  app.on('before-quit', (event) => {
    if (quitAfterServiceStop) return
    event.preventDefault()
    if(draftShutdownFailed){retryDraftShutdown();return}
    if (quitDiagnosticsFailed) {
      if (!draftCodecClosing) { quitAfterServiceStop = true; app.quit() }
      return
    }
    draftQuitDraining=true;draftRecovery?.quiesce()
    void verifiedQuit().catch(() => {
      void diagnosticRecorder.record({ source: 'main', event: 'graceful_shutdown_failed', level: 'error', details: { error_code: 'DEVICE_COMMAND_FAILED' } }).catch(() => {})
      showServiceQuitFailure()
    })
  })
}
